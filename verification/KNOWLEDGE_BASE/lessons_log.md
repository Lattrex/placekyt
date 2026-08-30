<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — per-block lessons log

Append-only, newest first. One entry per block (or per system-level campaign) as it
is verified: what was tried, what passed/failed, the derived tolerance, and any
block-specific gotcha. Promote anything that generalizes across block classes into
`invariants.md`. Entries up to 2026-08-13 were editorially consolidated (duplicate
and superseded material merged into the surviving entries; no durable lesson was
dropped) — append new entries above the oldest ones as before.

## ChaCha20KeystreamBlock — the release does not have a JUMP bug; it DEADLOCKS 2026-08-29 (pass 8)

**Headline: pass 7's diagnosis was wrong, and the evidence it said was missing
was already in `BuildResult`.** Two corrections, both worth more than the fix.

**1. `BuildResult` DOES expose the resolved per-cell image.** Pass 7 closed
saying "the missing evidence is the RESOLVED assembly and hop counts out of the
built bitstream, which `BuildResult` does not currently expose", and stopped.
It does expose them: `bres.chips[N].cells` is `{(x, y): {"entry", "memory"[32],
"face", "cell_id", "block", "routing_only", "classes"}}`. With
`_is_instruction_addr`'s rule (an address below `entry` is a data word) and the
v0.11 encoding (`op = word & 0xF000`; `HOP_CNT = bits[9:5]`, `@N = 31 - HOP_CNT`;
entry/dest = `bits[4:0]`), a ~15-line test-side disassembler prints any cell.
**No engine change was needed for the observability this pass was chartered to
add.** Cost of not looking: one whole pass.

**2. The jumps were never dead.** `bufB0.rel` resolves to
`[29] JUMP @8 entry=19` (19 IS `out`'s entry) and `[30] JUMP @1 entry=21`
(21 IS `bufA0.rel`). The words physically leave: the first release pair is
`0xe4e7`/`0xf110` = `0xE4E7F110`, RFC 8439 word 0, correct.

**The actual root cause: `stop_reason == "Deadlock"`.** The reorder band is one
eastward single-file row carrying two waves in OPPOSITE directions — the STORE
wave spills WEST (each A stage into its own B stage, once per drain lap) and the
RELEASE wave rides EAST to the egress. On the fourth drain lap they overlap and
two abutting cells each hold the word the other must accept:

```
bufA3 (9,0)  output_ready face=W -> neighbor 8 (bufB3)   # store spill, westward
bufB3 (8,0)  output_ready face=E -> neighbor 9 (bufA3)   # release word, eastward
```

Promoted to **INV-56**, with the head-on static check and its INV-4 mutant.

**THE METHOD LESSON, which is the reusable part.** When a block emits nothing,
**read `chip.run(...)["stop_reason"]` FIRST.** `completed` is `False` and the
word count is `0` for both failure modes, so neither tells them apart, but:

* `"QueueEmpty"` = ran to quiescence -> look at the PROGRAM;
* `"Deadlock"` = wedged in a circular wait -> look at the GEOMETRY.

Pass 7 read the trace and the word count, saw `bufB0.rel` execute and no output,
and concluded "the jump didn't land" — a program-layer story for a
geometry-layer fault. One field, available on every run, would have redirected
the entire pass. And the confirming experiment is one line: suppress ONLY the
release trigger and the chip flips to `"QueueEmpty"`.

**Diagnostic signatures worth reusing.** A FIFO whose stages do not all store
the same number of times has lost a word to a collision — here every stage
stored 4 times except `bufB3`, which stored 3. And a head-on resting-face pair
is findable statically from `_geometry()` alone, with no chip run, in ~10 lines.

**Measured dead ends** (all four `out` resting faces deadlock; removing
`bufA3`'s spill just moves the collision west; shifting the band one column west
hits `overlap` DRC; `bufA3` resting EAST breaks `bufA0.o0h -> out.v0h`). The
fix is a re-fold — separate the two waves in time or in space — and was out of
this pass's scope. **The block still emits nothing; do not use it.**

## ChaCha20KeystreamBlock — the reorder band is BUILT; the release jumps do not land 2026-08-29 (pass 7)

**What was executed.** The two-row re-fold pass 6 specified. The whole 10x6 fold
moved down one array row and a REORDER BAND went on top: each adder now feeds a
PAIR of depth-2 stages (`bufA_k -> bufB_k`), released stage by stage along one
eastward conveyor into the egress. 41 -> 48 cells, a 10x7 fold at array origin
(0, 0), still leaving five whole free rows.

**The three things worth carrying to another block.**

1. **A depth-2 pair is much cheaper than "half of depth 4".** Measured 22
   instructions / `base_addr` 9 / 8 live registers, against the depth-4 form's
   20/11/13 (an INV-33 overlap of three). The extra saving is that a two-slot
   cell emits BOTH its words from ONE straight-line entry, which deletes the
   release counter, the re-entry and the westward hop the depth-4 form needed.
2. **FIFO order IS pass order, so the reorder needs no schedule.** After the
   four drain laps `bufB_k` holds the older two words of output group `k` and
   `bufA_k` the newer two, purely because the pair is a 4-deep FIFO. All that
   has to be arranged is that the stages sit along the conveyor IN RELEASE
   ORDER; the "hold and release" is then the FIFO's own behaviour.
3. **A uniformly-faced line is a WALL FROM BELOW.** INV-55 rule 2 says such a
   band is sealed from outside; the converse bit here. The state line is a
   uniform EAST conveyor, so measured over every cell x every face, NOTHING in
   the control corner can climb through it. The only cells that can lift a word
   off it are the ones that already own an off-axis flip — the four taps. The
   fix was to put the chain's head where a tap's existing inward walk already
   lands (which is what fixes the reorder row's columns), costing zero new face
   constants, rather than to add a relay.

**Two relay routes measured dead, both on the register budget:** the write-back
cell is 22 instructions with its two face constants pinned at R8/R9 because the
eight frame words fill R0..R7; the row-trigger cell has 4 spare where a third
face constant plus its flip pair needs exactly 4. **Sharing a face constant
with a numeric one DOES work** and is the general trick — `EAST` is numerically
1, so it doubles as a decrement or compare operand (the `wbk` idiom). That is
what paid for the tap's relay entry.

**What is proven on the real placed + routed + built chip.** Everything up to
the release: 80 quarter-round invocations through all sixteen stages, 20
half-boundary realignments, 43/43/43 spins of rows 1/2/3 and 3 of row 0, all
four taps armed, the drain running four laps, **each adder firing exactly four
times, and every one of the eight buffer stages storing four times** — so the
band FILLS correctly on silicon — and the release trigger arriving
(`drn -> tap0.rel -> bufB0.rel`, both observed once).

**What does NOT work.** `bufB0.rel` executes and then neither of its outgoing
jumps lands: `out.default` never runs and `bufA0.rel` never runs, so the block
emits ZERO words (before this pass it emitted 32 correct-but-transposed ones —
the cipher is unchanged; the regression is confined to the new release path).
Ruled out BY MEASUREMENT, in order: the geometry (every walk resolves on the
block's own simulator, and the zero-exemption fold gate passes), the word budget
(all cells inside `base_addr`), the backward-JUMP rule (INV-53 satisfied and
gated), and the two-jumps-in-one-entry shape (with the chain baton removed
entirely the egress jump is equally dead). LAYER: block program / toolchain
resolution — FIXABLE. The missing evidence is the RESOLVED assembly and hop
counts out of the built bitstream, which `BuildResult` does not expose today;
getting at those is the next pass's first move.

**Gate status:** 33 of 34 in `test_chacha20_fixed_tap_ring.py`, 125 of 126
across the three chacha files; placement-legality, orientation, GRC-binding,
saturation, chip-scale and reachability all green (1016 + 78 + 24 passed). The
one failure is the on-chip value gate, which now asserts the sixteen words in
§2.3.2 order — the definition of done for this block.
## Poly1305MACBlock — the multiplier is SIGNED, which picks the radix; and a systolic ring CANNOT fuse adopt-and-forward into one entry 2026-08-29

First pass. **NOT done, and the manifest entry stays `planned`.** What IS
finished and measured: the golden (both implementations, all RFC vectors), the
arithmetic design, the block's `process_reference`, and — on a real placed +
routed + built chip — **the complete Poly1305 field multiply
`acc * r mod (2**130 - 5)`, all thirteen accumulators bit-exact over 11 cases
including the all-maximum corner.** What is NOT done is the rest of the block:
the message-to-limb packing, the carry-normalise phase, the final reduction and
`+ s`, and the tag egress. No end-to-end RFC tag has been produced on chip, so
nothing here is claimed as a working Poly1305 block.

**Every number below was MEASURED on a real placed+routed+built chip**, by
probe blocks driven through `run_block_dut_rate`, not derived from reading code.

### 1. `MUL`/`MULHI` are SIGNED, and that single fact picks the whole radix

`MULHI` is documented as "the high 16 bits" (PROGRAMMING_GUIDE §4.4), which
reads as unsigned. It is not. Measured, `x * 0xFFFF` for x = 1, 2, 0x7FFF,
0x8000, 0xFFFF returns the **signed** 32-bit product every time
(`0x0002 * 0xFFFF -> 0xFFFFFFFE`, not `0x0001FFFE`). With both operands
constrained to `[0, 0x7FFF]` the same pair is **exact and equals the unsigned
product** (verified across the range).

That constraint decides the limb layout, and it decides it *against* the plan:

* the radix must **divide 130 exactly** for the `2**130 ≡ 5` fold to be
  limb-aligned, which allows only 2, 5, 10, 13, 26;
* radix `2**26` (the plan's "five radix-2^26 limbs", and every textbook
  Poly1305) has 26-bit limbs — far outside 15 bits. **Not implementable here.**
* radix `2**13` has legal limbs but the folded coefficient `5*r[j]` reaches
  `0x9FFB`. Also dead.
* radix `2**10` / **13 limbs**: limbs ≤ 1023, `5*r[j] ≤ 5115`, both inside 15
  bits. Accumulators peak at a **measured 25 bits**, comfortably inside 32.

So the plan's suggested representation is wrong for this ISA, and the reason is
one measurement. LAYER: hardware/ISA — permanent. REACH: the signedness is a
property of the ALU and applies to every block using `MUL`/`MULHI`; the radix
conclusion is specific to Poly1305's modulus.

### 2. A 32-bit MAC is SEVEN instructions, and the obvious six-instruction order is silently wrong

`acc += a*b` on a hi/lo pair. The natural order —
`MUL / ADD / MOVE / MULHI / ADC / MOVE` — is **wrong**: `MULHI` is an ALU op and
sets all flags, so it **destroys the carry** the `ADD` just produced. Measured,
the accumulator carries a constant `+0x10000` error from the first
accumulation onward, **while the low word stays bit-exact in every one of six
successive MACs**. A gate that checks one word, or only the low half, sees
nothing.

The correct order computes the high half FIRST and parks it, keeping
`ADD`→`ADC` adjacent in flag terms:

```
MULHI c, a  /  MOVE t, R0  /  MUL c, a  /  ADD R0, lo
MOVE lo, R0 /  ADC t, hi   /  MOVE hi, R0
```

Verified exact over six successive accumulations of `921 * 5115`. Related and
worth stating alongside INV-45's "`ADC` is the carry — never synthesise it":
the park may be a `MOVE` **or a `{write}`** — measured, a `WRITE` between `ADD`
and `ADC` also preserves the carry. It is only the ALU ops that clobber it.

### 3. A hop-counted broadcast to N cells works, and it needs a COLLECTOR to observe

One cell writing to several downstream cells at hops 1, 2, 3 delivers three
**distinct** values correctly — the mechanism a systolic ring's coefficient
distribution needs. It measured as broken on the first attempt (all three sinks
held the first value) for a reason that is *not* the broadcast: each sink had
declared its own external `out` port, and the build re-resolved those into chain
hand-offs, rewriting the sinks' programs. Re-probed with the sinks reporting
into a single collector, all three values came back correct. **Only the last
cell may own the block's external output**; an intermediate cell that declares
one gets its program rewritten.

### 4. THE STRUCTURAL RESULT: a systolic ring cannot fuse ADOPT and FORWARD

This is the pass's most valuable finding, and it is a statement about the
substrate rather than about Poly1305.

The multiply is a cyclic convolution, which folds into a ring: cell `k` owns
accumulator `k` (resident — the accumulator must never move, or INV-45's
transport ceiling kills the design), the limb line ROTATES through the cells,
and the coefficient is broadcast. Verified equal to the plain dot-product form
over 400 random limb pairs, with the `2**130 ≡ 5` fold riding the limb line as a
single `×5` at the one wrap edge.

The natural cell program is one entry doing *adopt the predecessor's limb → MAC
→ forward to the successor*. **It is wrong, and it is wrong in all six
permutations of those three steps, in both trigger orders — twelve variants,
zero correct** (enumerated exhaustively against the reference). Measured on chip
first, as every cell's limb register holding the *same* value: a cell entry is
atomic, so whichever cell runs second in a pass already sees the first cell's
forward, and one value sweeps the entire ring in a single pass instead of
advancing one position.

**The fix is to STAGE the sweeps as two separate entries** fired by two separate
fan cells:

```
sweep 1 (entry `mac`)   :  acc += c * a ;  successor's a_in <- a
sweep 2 (entry `adopt`) :  a <- a_in
```

Because the whole ring finishes sweep 1 before any cell runs sweep 2, no cell
can observe the current pass's forward. Verified over 500 random limb pairs in
both trigger orders. Putting the MAC sweep first also removes the need to prime
the line or special-case pass 0.

Two cells rather than one because `1 MOVE + 13 WRITE + 26 JUMP` is 40 words
against a 31-word budget (INV-46: more cells doing less).

LAYER: hardware — a consequence of atomic cell entries, permanent. REACH: any
block whose datapath is a rotating line of cells that both consume and forward
the same value — systolic convolutions, ring accumulators, shift-register
folds. Stated generally: **on this substrate a systolic stage's "read old
value / write new value" cannot live in one entry; the read and the write must
be separated by a full sweep of the ring.**

### 4b. …and BOTH sweeps must fire in REVERSE ring order

Staging alone was not sufficient on chip, and the residual is a second,
independent fact. With the sweeps staged but fired FORWARD (mac0 first), twelve
of the thirteen accumulators were bit-exact and **the wrap cell alone was
exactly one pass stale**. Cause: a cell's `JUMP`s are issued in program order,
but the substrate is asynchronous, so "later in the entry" is not "later in
time" at a distant cell — mac12's closing write around the ring landed *after*
mac0's adopt. Firing the ring backwards puts the longest-latency trigger first
and the defect disappears.

The same fact killed a third variant: a dedicated `×5` wrap CELL between mac12
and mac0 cannot be ordered between the two sweeps by trigger placement at all —
its write landed one pass late no matter which cell fired it (tried from mac12,
from the MAC fan, and from the adopt fan). **Folding the `×5` into mac12's own
forward** makes it just another sweep-1 write, and those are proven visible to
sweep 2. That also keeps all thirteen MAC cells identical.

**And the egress must not ride a MAC cell.** Folding the drain onto mac6 (two
`is_face` words plus a flip-and-restore) shifted that cell's register map and
left **its accumulator, and only its accumulator, wrong** while the other twelve
stayed bit-exact. Twelve-of-thirteen is precisely the shape a one-value gate
cannot see — the brief's warning, met in practice.

**RESULT, measured on the real placed + routed + built chip:** with the sweeps
staged, both fired in reverse, the `×5` inline and the egress off the ring, all
**13/13 accumulators are bit-exact over 11 cases** — all-zero, `a=max r=max`
(every accumulator at its 27-bit peak, the full carry chain), `a=max r=1`, a
single limb, the `r=1` identity, and 6 random pairs.

### 5. The INV-33 overlap gate earned its keep twice, statically

The static per-cell budget check (`no data address, state register or pinned
input >= 31 - instr_count`) caught the wrap cell at **exactly one word over**
before any chip run — twice, on two different revisions. The fix both times was
INV-46's: move the work to another cell. The `×5` fold now lives in its own
2-instruction `wrapx5` cell on the ring's closing edge instead of costing every
MAC cell a data word and an instruction, which also made all 13 MAC cells
**identical**.

### 6. The collector must not sit on the ring either

Before that, the collector had been placed ON the ring, and it emitted
`0x3760376` — the broadcast coefficient `886` in both halves of every
accumulator. A cell on a walk is not neutral: it sat on the coefficient
broadcast walk, the two fan cells then disagreed about which walk position was
which MAC cell (one skipped distance 8, the other distance 9), and it was
triggered during a MAC sweep. INV-52 clause 2 from the other side. Moving it
off the walk fixed it.

### 7. The CARRY-NORMALISE phase: three more measured facts (second sitting)

The normalise reuses the same 13-cell ring — the carries rotate exactly like
the limbs, and a carry crossing the closing edge takes the same `×5`, so the
phase needs **no new cells**, only new entries and more fan sweeps. Validated
against the golden first (RFC §2.5.2, all nine §A.3, 400 random messages, at
just **2 rounds**; worst case measured at 2 rounds after the add and 3 after
the multiply). Then built and run on chip, which produced three findings:

**(a) A 27-bit accumulator cannot yield a 10-bit limb AND a one-word carry in
the same step.** The obvious split sends `hi*64 + (lo >> 10)`; for the true
maximum accumulator `68024385` that is `66430` — SEVENTEEN bits. The fix is
two stages per round: stage A carries the HIGH WORD (`≤ 1037`) and the
**receiver** applies the `×64`, so the wire never holds `hi*64`; stage B then
splits the 16-bit residue at bit 10. Worst carries measured 1037 and 386
against a 65535 limit. *This was caught by the on-chip case list including the
all-maximum corner — a random sample never reached it.*

**(b) `carry × 64` needs `MULHI` too — the same trap as the MAC, from the
other side.** `MUL` gives only the low 16 bits, and `1037 × 64 = 66368`
overflows, silently truncating to 832. Fixed with the same MULHI-first pairing
the 32-bit MAC uses.

**(c) A PROGRAMMED cell sitting on the ring's walk STOPS THE SWEEP DEAD.**
With the collector placed on the ring at walk distance 11, the fans reached
`c0..c6` and `c7..c12` were **never triggered** — and `stop_reason` was
`Deadlock` for some inputs and `QueueEmpty` for others, which is exactly why
INV-56 says to read it first. Replacing it with a face-only transit and
hanging the collector off the walk removed the deadlock outright. A face-only
`transit_*` cell forwards a hop-counted word untouched; a programmed cell does
not.

**(d) THE HARNESS BUG THAT LOOKED LIKE A BLOCK BUG (INV-1, again).** With
everything above fixed the ring still did nothing: **8 events** on the jump
run. The cause was the harness deriving the injection hop from a manhattan
guess (30) when the build had resolved it to 28, with the landing on a
different cell and a different entry. Taking `BuildResult.chips[0].
input_landings` instead — `{'cell': (2,0), 'entry': 25, 'hop': 28,
'data_addrs': [1]}` — turned 8 events into **1445** and every cell processed.
*Never derive the hop; read the one the build resolved.*

Where the normalise stands: it runs, all cases report `QueueEmpty`, and the
values land in the right range, with **3 of 9 cases exact**. The remainder are
short by exactly `carry_in × 64` (and cell 0 by `5 × 64`, the wrap) — i.e.
stage A's carry is read one sweep before it arrives, the same staging problem
already solved for the multiply, now to be solved for this phase. That is the
next step and it is understood, not mysterious.

### 8. Where the block stands overall

Proven on a real placed + routed + built chip: **the field multiply**
(13/13 accumulators, 11 cases). Running but not yet exact: the
carry-normalise (3/9). Not yet built: message-word-to-limb packing (its
bit-serial model is validated over 500 blocks), the final reduction plus
`+ s`, and the tag egress. **No RFC tag has been produced on chip**, so the
block is not done and the manifest entry stays `planned`.

### What is already gated

`verification/tests/poly1305_golden.py` ships two INDEPENDENT implementations —
a plain big-integer transcription and the 13-limb radix-`2**10` model the chip
computes in — and both reproduce **RFC 8439 §2.5.2** (including its published
intermediate `r` and `s`) and **all nine §A.3 edge-case vectors**, and agree
with each other over 500 random message/key pairs. The block's
`process_reference` runs the exact cell-level schedule (systolic passes, the
wrap-`×5`, the carry-normalise sweeps) and is exact against the golden on the
RFC vectors and 200 random word-aligned messages.

INTERFACE NOTE, honestly stated: the block consumes the message as 16-bit
words, so an ODD-length byte message is not expressible at this interface.
Three of the nine §A.3 vectors have odd byte lengths and are therefore gated at
the golden, not at the block.

## ChaCha20KeystreamBlock — the emission-order fix is at the COLLECTOR, and it misses this fold by exactly three words 2026-08-29

Sixth pass. The block arrives functionally correct — all sixteen RFC 8439 §2.3.2
state words bit-exact on the real placed+routed+built chip — with one defect: the
32 words leave in LAP-MAJOR order, the 4x4 transpose of §2.3.2's. **No code
change shipped this pass. What shipped is six gates that convert four passes of
argument into measured fact, and a re-fold specification verified walk by walk.**

**The brief asked me to test an assumption, and the assumption was right to
doubt.** Every previous pass looked for the fix at the ROWS — a per-row loop
draining one row completely before the next — and recorded "does not fit". That
finding stands, but it turns out to understate the case, and the search was
aimed at the wrong end of the block.

**What I measured, in the order I measured it.**

1. **The boot-time permutation (the brief's "check this first") is DEAD, and for
   a stronger reason than a budget.** It looked like the cheapest possible fix:
   loading row `k` with a permuted slice costs zero instructions and zero cells,
   just different `initial_value` constants. I modelled the ring exactly —
   validating the model by reproducing the known defect's permutation
   `[0,4,8,12,1,5,...]` — and then *derived* the load map the quarter-round
   schedule demands rather than guessing candidates. **Every one of the sixteen
   (row, slot) cells is pinned, with no conflict and none left free, and the
   unique solution is the identity the block already ships.** There is no
   boot-time permutation to choose; the QR wiring has already chosen it. The
   brief's instinct that pass 5's "relabelling changes values not order"
   argument didn't obviously transfer was correct — but the conclusion survives
   on its own merits, by a different route.

2. **Nor does any drain-side knob.** Exhaustive over all `4^4 x 4^4 x 4!`
   combinations of pre-drain rotation, inter-lap spin count and row publish
   order: none gives §2.3.2 order, best 4 of 16 positions. The structural
   reason, which generalises: **one drain lap visits each row exactly once, so
   the row index is the fast-varying half of the output position while the state
   index carries it in the slow nibble — no permutation of laps or rows can
   exchange them.**

3. **The COLLECTOR avenue is real and the reorder is small.** Emission position
   `4L + k` carries `state[4k + L]`; read the other way, the word wanted at
   output position `4k + L` is the one `add_k` produces on lap `L`. So **output
   group `k` is exactly `add_k`'s four words in lap order**, and the entire 4x4
   transpose is *hold each adder's four words, release adder by adder*. That is
   a per-ADDER buffer, not a per-row loop, and it needs no counter reaching the
   rows. The brief's "work out the actual minimum buffering depth" was the right
   question and the answer is better than the naive one: a single streaming
   reorderer would need 9 words held at peak, but **split per adder it is 4
   words each**, and the cell shape is exactly `_row`'s, already proven on chip
   at that depth. Simulated at the cell-instruction level it emits §2.3.2 order
   exactly.

4. **The gap is THREE INSTRUCTION WORDS, measured both ways.** The buffer needs
   10 live registers (8 for four 32-bit words + 2 for the arriving pair), an
   8-instruction shift (irreducible for depth 4), and a release that emits one
   word and re-enters three times. Without the release counter: 16 instructions,
   `base_addr` 15, 12 live words — **three spare**. With it: 20 instructions,
   `base_addr` 11, 13 live words — **overlap of three**. The counter's
   `SUB`/`MOVE`/`BR` triple *is* the shortfall.

5. **I could not relocate the counter, and I measured why: the finish row is
   SEALED.** Over every free slot of the fold's bounding box x all four faces,
   **no cell of the finish row reaches ANY free slot on ANY face.** North leaves
   the block (the array row above is the I/O corridor); south lands on the state
   line, whose cells all rest EAST — a solved constraint serving `wb`, `wbk` and
   `drn` — so the word is swept along it and out; east/west stay on the row.
   **This is the general form of pass 5's "`d3` has ZERO candidates": that was
   true but attributed to `tap3`'s position, when the real cause is that the
   whole band is enclosed.** The adders were separately measured at zero free
   data addresses (four after splitting their output into `oh`/`ol`, which saves
   an instruction) — still short of what a counter plus re-entry needs.

**The re-fold that WOULD work, verified rather than sketched.** Shift the whole
10x6 fold down one array row and add a second buffer row on top, giving each
adder a PAIR of depth-2 buffers (a 4-deep FIFO as two stages). Depth 2 frees four
registers per cell and the counter fits with room to spare. The array is 10x12
and the block sits at array row 1, so there is space; 41 → 49 cells. **Every
existing control walk survives the shift** — the state line's hop-1/3/5/7
broadcast from both `wbk` and `drn`, the tap-to-adder abutment, the
`wb → seq → wbk` hand-off — and the one new walk, `bufA_k → bufB_k`, is hop 1
north. All of that is gated, so the next pass executes rather than re-searches.

**Gotchas worth carrying forward.**

* **A partial implementation was written and reverted.** I built the four-buffer
  version end to end — cells, geometry, wiring, faces — precisely to *measure*
  the budget rather than derive it, and the resolver reported the overlap. That
  is the intended use of a throwaway: the number is now a gate, not an opinion.
* **The adder's output was one port for both halves.** Feeding a buffer needs hi
  and lo in different registers. Splitting `out` into `oh`/`ol` with a single
  trailing trigger costs nothing (a `WRITE`'s target is an instruction field)
  and **saves** an instruction — worth keeping in the re-fold.
* **`arm` falls through.** `tap0`'s `arm` entry ends in a remote `{jump:narm}`,
  and a remote JUMP does not stop local execution (INV-43 rule 2), so any entry
  appended after it needs an explicit `HALT` in front or it fires during arming.
* **ENVIRONMENT (cost me real time, and cost pass 5 more).** In a git worktree
  the venv resolves `gr_kyttar`/`simkyt` to the MAIN checkout, so edits appear to
  do nothing and tests appear to pass. Set
  `PYTHONPATH=<worktree>/runtime/python`. Verify with
  `import gr_kyttar...; print(module.__file__)` before trusting any result.
* **The on-chip gate is genuinely fast (~0.4 s) and genuinely real** — I
  mutation-tested it (drain lap count 4→3) and it caught the mutant while still
  showing 640 `in0` executions. A quick green is not evidence of a skip here.

**Gates added (6, each with a proven INV-4 mutant):** the forced boot load map;
the exhaustive drain-knob search; the per-adder buffer producing §2.3.2 order;
the three-word budget gap; the sealed finish row; the two-row re-fold's walks.
Mutant 3 reproduces the block's actual defect `[0,4,8,12,...]`, so the gate
discriminates the real bug and not a proxy. Full suite: **1220 passed, 0 failed**
(chacha 34, plus saturation, GRC-binding, orientation, placement-legality,
chip-scale, reachability). The fold gate still runs with **zero exemptions**.

**STATUS: still `needs_human`.** The cipher is correct; the order is not; the
remaining work is a re-fold whose geometry is now proven and whose cell budget is
now quantified. **LAYER: block program / fold — fixable, not a substrate limit.**

## ChaCha20KeystreamBlock — the cipher is CORRECT on chip: all 16 state words bit-exact. Two "verified" facts from the last pass were the two remaining bugs 2026-08-30

Fifth pass. The block was handed over as *"four words short of done"*, with a
strongly-indicated fix (add a cell for the drain) and a named blocker (only `wbk`
at `(1,0)` can reach all four rows). **The drain was indeed a cell, and the
handoff's headline advice was right. But the four-word shortfall was not the real
constraint, and two things the previous pass had recorded as PROVEN CORRECT were
the two actual defects.**

Where it now stands, on the real placed + routed + built chip: 80 quarter-round
invocations through all sixteen stages, **20** half-boundary realignments, 40
realignment spins of each of rows 1/2/3, four taps armed, each adder firing four
times, **32 words emitted**, and **all sixteen state words BIT-EXACT** against
RFC 8439 §2.3.2 — not word 0, every one of them.

Still `needs_human`, for a genuinely smaller and fully-characterised reason: the
32 correct words come out **transposed**. See the last section.

### The two defects were both recorded as correct behaviour

**1. There are TWENTY realignments, not nineteen.** Each diagonal half is
bracketed — row `k` spun `k` times before it and `4 - k` after — so ten double
rounds need ten openings and ten closings. Nineteen fall between laps; the
twentieth is the closing bracket of the *last* diagonal half and has no following
lap to hang off, so it was never issued. On chip the spin counts were 37/38/39
where the schedule requires 40/40/40 — exactly `10a + 9b` against `10a + 10b`.

The previous pass's on-chip gate asserted `wbk.bnd == 19` and `row{1,2,3}.spin ==
37/38/39` **as "the exact counts RFC 8439's 20 rounds require"**, and asserted the
value of word 0 alone. Every assertion passed. The recorded numbers were the bug,
written down as the specification.

It hid because **row 0's bracket is zero spins either way**. Row 0 stayed aligned,
and row 0's head is the RFC's first output word — so `0xE4E7F110` came out
bit-exact while rows 1, 2 and 3 were each rotated by `4 - k` too little and
drained slot `k` instead of slot 0. A gate on word 0 could not see it. Promoted:
*a value gate must cover the degenerate element (zero-width bracket, unit
coefficient, zero shift), because that is the one that stays correct under the
bug — and it is very often the one "check the first word" lands on.*

The fix cost nothing. `wbk.bnd` already alternates halves on a toggle, and on the
twentieth entry the toggle is already even, so `seq.finish` simply fires it once
more before arming the drain. No new schedule logic — and it gave `finish` the
face restore it had previously been *exempted* from needing, so the fold gate now
runs with **zero exemptions**.

**2. A backward JUMP had been silently redirected for a whole pass, hidden by an
address coincidence.** `build._apply_internal_feedback` resolves a backward
internal jump by rewriting the source cell's **highest-addressed `JUMP`** —
whichever instruction that is; it does not match the port name. `wbk` has one
backward jump (`step → seq.step`), but its highest-addressed jump was `back →
row0.pub`. So the build rewrote `back` to point at `seq.step`. Straight out of the
built words: address 30, `0x73d2` = `JUMP dist=1 entry=18`, and `row0.pub` is 15.

**It ran anyway, because `seq.step` and `row0.pub` both resolved to 15.** The
corrupted jump landed on the right entry by pure numerical coincidence.

This is the same hazard the previous pass wrote down from the other side —
*"dropping `seq`'s redundant `MOVE half, four` FITS AND BREAKS THE BLOCK"* — and
read as "shortening a cell can break an internal edge". That is the symptom. The
cause is that **the edge was already broken and the collision was masking it**;
shortening `seq` did not break anything, it *revealed* it. I hit exactly this: a
three-instruction saving in `seq` moved `seq.step` to 18, and the realignment's
hand-back started firing into the lap counter. 80 laps still ran, `wbk.bnd` still
fired 20 times, `row0.pub` dropped from 84 to 64 — and every drained word was
wrong.

Fixed by emitting `wbk`'s `default` entry **last**, so `{jump:step}` is the
highest-addressed jump — which is what the build's rule actually requires. That
cost one `HALT`, recovered by hoisting the spins common to both half-boundaries
(row1 once, row2 twice, row3 once) out of the branch and keeping only the two
that differ: six jumps and one branch where there were twelve jumps and two
schedules.

Both clauses are now static gates, and so is the collision that masked them.

### The handoff's blocking claim was wrong, and measurably so

The dispatch recorded that the drain rotate *"must come from the only cell
reaching all four rows on one walk (`wbk` at (1,0))"*. An exhaustive search over
every free slot × every face finds **nineteen** slots whose walk reaches all four
rows in order. That claim was derived, not measured — and the campaign's own rule
("every 'cannot' that was DERIVED was wrong; every one that was MEASURED held")
called it correctly for the fifth time.

So the four-word shortfall was never the binding constraint. The drain went to
`drn` at `(1,2)` resting NORTH: rows at hops 1/3/5/7 and `row0.pub` at that same
hop 1, so **all five of its jumps ride the resting face with no flip at all** —
and therefore need no restore. The lap closes `tap3` → (SOUTH, hop 19, straight
through the fourteen idle quarter-round stages, which are transparent to a
hop-counted word) → `add_pad`, a paving cell that already had to exist and had 26
spare words, which turns it one hop east into `drn`. INV-46, exactly as
`LZ4DecoderBlock` did it. 41 cells, one more than before.

**Program order is a design lever.** `drn` fires five triggers at the state rows
and is listed *before* them in `build_cell_programs`, because whether a jump is
"backward" is decided entirely by that order — and a cell with five backward
jumps keeps one and silently drops four. Listed first, all five are ordinary
forward handoffs the resolver sizes itself.

### What remains: the words are right, the ORDER is not

One drain lap empties one **slot** of every row, so the words leave lap-major —
`state[0], state[4], state[8], state[12], state[1], …` — the 4×4 **transpose** of
§2.3.2's order. Every value is bit-exact; the positions are permuted. A keystream
consumed in the wrong word order is a different keystream, so this is not
cosmetic and the block stays `needs_human`.

Emitting `0..15` needs the slot index to advance fastest and the row slowest —
one row drained completely before the next starts — which is a per-row loop with
a per-row counter. **All three places it could live were measured, and none fits
this fold:**

* the **row** cell has four spare words but **no free data address** for the loop
  constant: its eight state registers occupy 3..10, and the next free address
  collides with `base_addr` once the four loop instructions are added;
* the **tap** cells have 2–3 spare against a cost of about seven;
* a per-row sequencer **cell** was searched exhaustively over **all seventy** free
  slots of the 10×11 region × all four faces. `d0`/`d1`/`d2` have candidates;
  **`d3` has zero** — `tap3` sits at the east end where the quarter-round chain
  begins, and nothing `tap3` can reach can in turn reach `row3`.

Relabelling cannot absorb it either: the adders fire in the order their taps
publish, so permuting which adder serves which row changes the *values*, not the
order. Three independent arguments, all agreeing.

**LAYER: block program / fold.** Not the substrate, not routing, not arithmetic.
A fold in which every tap can reach a cell that reaches its own row would fix it;
this one — with the quarter-round chain starting at the east end of the state
line and folding back across every column — cannot. That is the next pass's job,
and it is a re-fold, not a re-design: the fixed-tap ring, the realignment and the
drain are all now proven correct on silicon.

### Method note

The previous two passes each sharpened one lesson: *"a model that flatters the
design is worse than no model"*, then *"reading `MOVE [FACE]` is necessary but not
sufficient — iterate to a fixpoint"*. This pass adds a third turn of the same
screw, and it is about **gates rather than models**:

> **A gate that asserts the numbers you observed is not a gate.** Both of this
> pass's defects were sitting inside assertions that passed — one as "the exact
> counts the schedule requires" (they were not), one as an address equality nobody
> had reason to question. Derive the expected value from the algebra (`10a + 10b`),
> assert *all* the outputs rather than the first, and treat a numerical
> coincidence between two entry addresses as a thing to prove impossible, not a
> thing to rely on.

Two candidate invariants, assigned at landing as **INV-53** and **INV-54** in `invariants.md`: the
backward-jump-by-address rule (which **closes INV-52 clause 5**, previously
recorded as open), and the last-closing-bracket rule.

## ChaCha20KeystreamBlock — the ring RUNS: all 80 laps and state word 0 bit-exact on chip. Every defect was a FACE, and the router could not see any of them 2026-08-30

Fourth pass, and a bounded one: the block was handed over as "one symptom away"
— *places, routes and builds clean, emits no words, the ring never starts*. It
now runs **the whole of RFC 8439's schedule on a real placed + routed + built
chip**: 80 quarter-round invocations through all sixteen stages, 19 half-boundary
realignments, 37/38/39 realignment spins of rows 1/2/3, all four taps armed for
the finish — and **state word 0 comes out bit-exact, `0xE4E7 0xF110`**. A wrong
20-round permutation cannot produce those bytes, so the datapath, the write-back,
the fixed tap and the realignment are all confirmed correct on silicon.

Still `needs_human`, for a much smaller and fully-characterised reason: the drain
does not repeat, so 8 of 32 words come out. See the bottom of this entry.

### Every single defect was the same class, and it was candidate cause #2

The handoff listed four candidates. It was **INV-48 root cause C — a face that
misses — five times over**, in five different disguises. Not a missing `HALT`,
not boot ordering. INV-50 was involved, but as an *accomplice*: it is what let
all five hide.

**1. Face CONSTANTS pointing the wrong way.** Four cells had `is_face` DataWords
naming the wrong compass direction outright — `wbk`'s row triggers NORTH, which
is off the top of the array; the four taps' inward flip SOUTH when their adders
are NORTH; `wb`'s hand-off SOUTH when `wbk` is NORTH; `wbk`'s lap-advance EAST
when `seq` is WEST. **This one constant — `wbk`'s `f_ring` = NORTH — is the whole
of "the ring never starts".** `wbk` never executed at all in the trace: zero
events, because `wb`'s jump to it left on the wrong face.

**2. The FACE register PERSISTS across entries, and an entry that does not set it
inherits whatever the last path left.** `wbk.default` flipped WEST for the lap
advance and never restored, so the *next* lap's four row triggers fired west into
`seq`. `seq.default` flipped SOUTH and never restored, so the boundary hand-off
fired south into `wb`. The discipline that fixes it is one line: **every path
restores the resting face before it ends, so every path may assume the resting
face on entry.**

**3. A cell's flip also deflects words that merely TRANSIT it.** This was the
sharpest one. `wb` sits at `(0,1)`, directly on `seq`'s walk down to the state
line, and left its face pointing north at `wbk`. Every `pub` trigger `seq` issued
therefore bounced off `wb` straight back into `seq`, which re-entered `step`,
decremented the lap counter again, and ping-ponged. Measured: the ring completed
exactly ONE lap and then oscillated, with no output and no error. **A cell's
resting face is a contract with every walk that crosses it, not just with its own
edges** — and `seq`'s own resting face is FORCED to EAST by `wb`'s jump needing
to transit it, which is why `seq` pays for a flip on every one of its own edges.

**4. An internal edge the block never declared.** `in0.trig -> in1.default` comes
straight from `ChaCha20QRBlock` and was simply absent from `internal_jumps()`,
while the assembly still emitted the word. It happened to resolve correctly; it
is now declared.

**5. The router faced `tap3` at its ADDER and sized `tap3.q -> in0` at three
hops.** `tap3` has internal connections to both `in0` (east, 1 hop) and `add3`
(north, 1 hop); `router._place_block_cells` faces a cell at "the" declared
destination, picking whichever the dict yields, then `_get_routing_distance`
walks *that* face. It found the real path `tap3 -> add3 -> out -> in0` and
returned 3. At run time the word leaves EAST and overshot `in0` by two, landing
in `l1_add`. **The effect: every frame reached the collector TWO WORDS SHORT**,
`in1`'s mod-8 counter never hit a frame boundary in step with the ring, and the
whole cipher ran five laps and stalled. This is the single fix that took the
block from 5 laps to all 80.

### THE MEASUREMENT THAT MATTERS: the router cannot size a FLIPPED edge

Classifying all 233 internal edges by whether the emitting path is on the cell's
resting face or a flipped one:

| emit face | resolved correctly | resolved WRONG |
|---|---|---|
| resting | **211** | **0** |
| flipped | 16 | 6 |

**Every flipped edge that works does so by coincidence.** `_get_routing_distance`
walks resting faces only; for a flipped emit it either (a) misses, falls back to
Manhattan, and Manhattan happens to equal the true distance — which is how all 16
"correct" ones pass, every one of them a straight-line hop of 1 or 2 — or (b)
*succeeds spuriously* on a path the word never takes, which is what killed
`tap3`. This is INV-50's real shape: the Manhattan fallback is not the only
failure mode, and it is not even the worst one. A successful walk from the wrong
starting face is worse, because nothing looks anomalous.

### Two toolchain fixes, both regression-tested at the 1219 baseline

* **`router._place_block_cells` looked programs up by the raw positional INDEX**
  (`if i in block_def.cell_programs`), which is False for every string-keyed
  block — the DFE's `ff0`, ChaCha20's `tap3`. So a string-keyed block silently
  got no program copied there and, sharply, **never had its declared `fwd_face`
  honoured**; the router kept its own guess and sized every edge against it.
  Fixed to look up by the positional KEY, falling back to the index.
* **`_get_routing_distance` gained an optional `start_face`**, and blocks may now
  declare `emit_faces() -> {(cell_id, port): neighbour_id}` for ports they emit
  while flipped. The value is a **cell id, not a compass direction**, so the
  router derives the face from the two cells' PLACED coordinates — which the
  placer has already rotated. That is what keeps it orientation-correct by
  construction (INV-23) instead of needing the hand rotation that regressed
  `test_rotated_feedback_block_computes_identically` last time. `placekyt/tests/`
  is **1219 passed**, the orientation suite green, and the ChaCha20 + binding +
  legality + saturation suites all green.

### A model that flatters the design, part two

The previous pass's lesson was that a fold checker must read `MOVE [FACE]` out of
the real programs. That is necessary but not sufficient. The checker written this
pass **symbolically executes every path, over both sides of every branch, and
iterates the face register to a FIXPOINT** — seed with the resting face, collect
what each path can *leave behind*, re-seed, repeat. A checker that assumes each
entry starts clean is exactly a checker that cannot see defect 2 or defect 3, and
those two were half the bugs. It is now `test_every_internal_edge_lands_on_a_real
_forwarding_walk` with an INV-4 negative that re-points the original constants and
asserts each of them misses.

### What remains — measured, and it is a WORD BUDGET, not a wall

Each row holds four 32-bit words and one drain lap emits the head of each, so the
finish must run four laps with a plain rotate (`row.spin`) between them. The
rotate must come from a cell that reaches all four rows on ONE walk — on this fold
that is only `wbk` at `(1,0)` — and the lap must be closed by a cell that can
reach `wbk`, which is only `wb` or `seq`: **the tap line and the finish row both
run one-way AWAY from the control corner**, measured over all four faces from
every tap, every adder and the egress. That costs a `drn` entry on `wbk` (5
words), a relay entry on `wb` (3) and a lap counter on `tap3` (6).

**Shortfall: four words**, after compressing `wbk`'s realignment from twelve jumps
to eight by hoisting the spins common to both halves (row1 x1, row2 x2, row3 x1 —
verified behaviour-identical: the spin counts stayed 37/38/39). LAYER: block
program / fold. Not routing, not arithmetic, not the substrate.

### The trap that cost the last hour, and is worth its own line

Freeing that word by dropping `seq`'s `MOVE half, four` (redundant — `half` is
`reset_per_batch`) **fits, and breaks the block.** Shortening `seq` moves
`seq.step`'s entry address from 15 to 14, and the build then mis-resolves
`wbk.back` to it instead of to `row0.pub`, which is also at 15. The realignment
ran perfectly and then handed control to the lap counter; the ring stopped at the
first boundary with a flawless trace up to that point. **Entry addresses are
params-dependent (INV-6/11) and that hazard reaches INTERNAL edges too** — so
changing a control cell's LENGTH can silently re-target another cell's jump. Any
attempt at the four words must re-check `wbk`'s built words, not just the budget.

---

## ChaCha20KeystreamBlock — the SELECTOR was unnecessary: a fixed-tap ring makes the permutation a shift register. Places/routes/builds, does NOT yet compute 2026-08-29

Second re-examination. **Outcome: still `needs_human`, and again for a smaller
reason than before.** The architecture the previous pass settled on — 8 lane
cells with a `LOAD`-indirect read, a 4-way `CMP`/`BR` write-back and a
fan-out-8 selector broadcast — turned out to be unnecessary. The block is now
authored, wired, folded, placed, routed and built, all static gates green, and
it **emits no words**: the ring does not start. What remains is control-flow
debugging on chip, not architecture.

The headline, because it removes a whole mechanism from the design space:

> **The ChaCha20 round permutation is a SHIFT REGISTER, not a selection.**
> Written as `index(k) = 4k + ((j + k*shift) & 3)` it invites a per-row
> selector. Written as a per-row READ OFFSET it collapses to "every row taps
> slot 0", and the entire column/diagonal permutation becomes a rotate.

### The restatement

```
row 0 reads offsets  0 1 2 3 | 0 1 2 3
row 1 reads offsets  0 1 2 3 | 1 2 3 0
row 2 reads offsets  0 1 2 3 | 2 3 0 1
row 3 reads offsets  0 1 2 3 | 3 0 1 2
```

Every row reads **offset 0** provided it rotates left by one after each quarter
round. The column half needs nothing else. The diagonal half is the *same*
sequence started `k` positions later, so it is bracketed by `k` extra rotations
of row `k` and `4 - k` to restore alignment — and a 4-slot rotation has order 4,
so those brackets cancel over a double round.

What that deletes: the selector broadcast (fan-out 8), the `LOAD`-indirect read,
the 4-way write-back branch, and the 8-cell demux. **40 cells and 58 internal
edges** replace 37 cells and 36 much harder ones, and every remaining edge is a
plain walk.

Proven EXACT against RFC 8439 §2.3.2 (state **and** the 64 keystream bytes) and
§2.4.2 (the full 114-byte, two-block encryption vector), over the same
cell-level operations the hardware performs — publish / quarter round /
write-back-and-rotate / spin. Gated with 8 INV-4 mutants, of which four are the
dangerous kind: **diagonal-half-first, no-realignment, reversed spin direction
and a stuck tap all still perform exactly 80 invocations.** No count-based or
structural check catches any of them; only a value gate does. Same class as the
counter-direction mutant the previous pass found.

### Measured on the real chip this pass

1. **The row cell.** Publish the head pair, install a written-back replacement,
   rotate the four 32-bit slots: correct on silicon, 4 spins is the identity,
   16 instructions with 5 spare words.
2. **INV-48's forwarding rule, live — as a failure and then a fix.** A cell
   whose resting face does not reach its target produces **no output and no
   error**; an in-program `FACE` flip fixes it. This was watched happen, not
   inferred.
3. **A word transits cells that carry a FACE and NO PROGRAM**, at distances
   2/3/4/6. But the build gives a bare array cell a face only where a ROUTE
   claims it — so an unoccupied column inside a block's **own footprint** is
   still a dead end for a block-internal `WRITE`. Both halves matter when
   folding, and getting only the first half right is what made an early fold
   look feasible when it was not.

### The new silent trap, now gated: POSITIONAL PAIRING

`build_cell_programs()` and `default_layout()` must **iterate in the same
order**. The router and the build walk the programs and the placed cells in
lockstep *by position*; both dicts are keyed by cell id, which **hides** a
mismatch. The design places, routes, builds and DRCs clean and whole cells come
out with **empty memory**. Symptom: a block that builds green and emits nothing.
Cost here: one debug cycle. It is INV-33's positional-pairing clause, and it now
has a test.

### The three structural facts that fixed the fold

* **The state line must be COLLINEAR and CO-FACING.** `wb` (eight write-backs),
  `wbk` (four rotate triggers) and `realign` (the boundary spins) each have to
  reach several rows from ONE walk, and a walk serves several targets only when
  they are consecutive along it — the `LMSEqualizerBlock` broadcast idiom.
* **The finish row must be GAP-FREE**, because the four adders share one walk
  into the egress. Hence three pass-through cells paving between them.
* **A CLOSED RING TRAPS ITS INTERIOR.** Every ring cell forwards along the ring,
  so a word emitted inside it in *any* direction joins the ring and follows it
  forever — there is no walk from the inside out. A rectangle-perimeter fold is
  therefore wrong for any block whose interior must reach an edge; the fold is a
  serpentine with free ends instead. Measured on the fold, not derived.

Also worth reusing: the **initial state is a build-time constant** (the four RFC
constants are fixed; key, nonce and counter are block parameters), so the
add-back needs no shadow copy of the state — and each adder holds its four
addends in a **rotating** register so the add-back tap is *also* always slot 0.

### What is NOT done — and why the source is committed anyway

The block places, routes and builds at 40 cells in a 10×6 fold; every cell is
inside its 31-word budget; all 58 internal edges verify against a walk simulator
that credits a `FACE` flip **only** to cells whose programs actually contain
one. And it **emits no words**. The rows boot correctly seeded with the RFC
initial state, `seq`'s external trigger arrives at the right entry with a
correctly resolved hop, and no lap ever runs.

The source is committed this time — unlike the previous pass — because it
places, routes, builds and is statically green, so the next agent can start from
a running toolchain rather than a description. **It is not marked `done` and it
does not compute.** Start at why `seq`'s `default` entry does not get the ring
turning in the built bitstream.

### Method note

The walk checker I built to search folds initially credited a face flip to any
cell I nominated. It passed a layout that then mis-resolved on silicon, because
**a flip only exists if the cell's program actually contains `MOVE [FACE]`**.
Rewriting the checker to read the programs rather than a hand-maintained list
turned three "green" edges red immediately — the same failure the KB keeps
recording in a new costume: a model that flatters the design is worse than no
model. Every layout claim in this entry comes from the checker that reads the
programs.

## LZ4DecoderBlock — the cell cap was FICTION; it now PLACES and BUILDS clean, and the real blocker is measured 2026-08-29

Re-opened from the quarantine below, which cited a panel-template **cell cap** that
does not exist. Outcome: **still not `done`, but the wall is a different, smaller and
fully measured one**, and four toolchain bugs were found and fixed on the way.

### The quarantine's three "walls", each disposed of

* **"the panel template caps cell count"** — FALSE. `engine/panel_pnr.py` has no
  `cell_count` check of any kind; its only count limit is `len(backed) > 2`, which caps
  how many *blocks* may be panel-backed. `GolayDecoderBlock` had already shipped as a
  7-cell panel block. **Measured extra fact the audit did not have:** Golay fails the
  *same* template with a `TypeError`, because the RX shape writes Varicode-specific
  params into `blk.params`. The template was broken for both, and for neither reason
  the quarantine gave.
* **"no single-face ring ordering exists"** — true but the wrong question. It searched
  RINGS, where every edge runs the same way round a cycle.
* **"the template rejects this block"** — asserted only that *something* raised, with no
  message check. The audit already showed the same shape raised for the working
  3-cell Varicode decoder.

### The substrate rule that actually governs this (now INV-48 root cause C)

A word leaves on its SOURCE cell's face, and every cell it then arrives at forwards it
on **that cell's own** face. Each cell therefore has exactly ONE outgoing walk. Read off
a simkyt trace, not inferred. The straight-line "ray" model is false, and it is what
produced a 7×1 row that places clean, builds clean, passes DRC — and **hangs**, because
the router faces west into an emit cell facing east and the word ping-pongs.

**Fan-out is not the wall**, checked against the library before saying so:
`LMSEqualizerBlock` ships with fan-out 6 and 5 backward edges, `FFT64Block` with fan-out
4 and 6 — both strictly worse than LZ4's (4, 3), both `done`. They win by putting a
cell's targets CONSECUTIVELY along one walk, and by authoring in-program FACE FLIPS
(`DataWord(is_face=True)` + `MOVE [FACE], …`), which cost 2 instructions + 1 data word
per extra direction.

### The remaining blocker, with its arithmetic

A ring over cells 0..5 with the controller OFF the ring satisfies all 14 inter-cell
edges in the natural order (exhaustive). The leftover edge `emit → controller` needs a
face flip; the emit cell has **2 free words against the 3 a flip costs** (`token` has 1).
Exhaustive over every 4×2…7×2 fold with per-cell budgets: no arrangement every cell can
afford. The way through is measured too — the per-byte `set_addr` is redundant (the
controller's `write` auto-increments its own `wraddr`; equivalent to the golden on 19
payloads) and frees exactly 3 words — but it changes the program the match copy is
proven on, so it needs its own silicon re-verification and was not taken.

### What is NOW green (do not redo)

* `_apply_self_contained_template` (new, `engine/panel_pnr.py`): places **all 7 cells**
  from the block's own `default_layout` with the controller pinned on `x1_out`, draws
  the input/return/egress corridors, derives the push-read descriptors. **0 DRC errors,
  0 warnings**; the build binds every program to its placed cell.
* The role-named templates place ONLY the cells named in `panel_requirements()`. For a
  larger block that is silent-dead twice over: the extra cells get no position, AND the
  build binds programs to `placement.cells` BY INDEX, so the survivors land on the wrong
  positions. New `self_contained` + `return_entry` requirement keys.
* `placekyt/tests/` **1219 passed**; Golay + Varicode suites **92 passed**.

### Four silent toolchain bugs, found and fixed

1. `router._find_output_target` returned the target's DEFAULT entry for a port carrying
   both a WRITE and a JUMP — so the emit cell's `set_addr`/`write`/`lookup` hand-offs all
   fired the controller's *first* entry. Now reads the entry from `internal_jumps`.
2. `router._fixup_write_instructions`'s sink fallback rewrote EVERY WRITE/JUMP in the
   exit cell to the port hop, aiming the panel protocol at `x16_out`. Now honours
   `RAW_OUTPUT_HOPS` via a new `BlockDefinition.raw_output_hops`.
3. `build._apply_output_port_routes` lacked the `RAW_OUTPUT_HOPS` guard its sibling
   `_apply_routes` has. Same failure, different pass.
4. `build._patch_one_handoff` matches by destination REGISTER alone and takes the
   lowest-addressed hit, so a cell driving two different cells' registers that share a
   number patches the wrong instruction. Worked around by pinning cell 4's `mat` off
   cell 0's `st`; the pass is still ambiguous and deserves a port-aware fix.

Also corrected: the block declared no `output_cell_id()`, so the port map put its output
on the *controller*; and its docstring repeated the false "x1 is one bit wide" claim
(x1 is a SERDES — `width: 1` is a PIN COUNT).

### Test suite

41 passed, 4 skipped. The three `test_placement_wall_*` gates are gone; the header of
LAYER 5 records what each got wrong. New gates pin placement, the build binding, the
exact set of unroutable edges, and the flip budget — the last two fail the day the gap
closes, and the written-and-ready end-to-end decode test is skipped until then.
## ChaCha20KeystreamBlock — RE-EXAMINED: two of the three quarantine walls were WRONG, and the block needs no SRAM panel 2026-08-29

Re-opened the 2026-08-29 quarantine of the RFC 8439 §2.3 block function. **Outcome:
still `needs_human`, but for a completely different and much smaller reason** — the
architecture is settled and every mechanism is measured on the real placed+routed
chip; what remains is wiring, not architecture. Promoted to **INV-49**, and INV-47
is corrected in place (twice).

The headline, because it is being used to size a future architecture:

> **"The state cannot transit a cell" is FALSE.** It was algebra over INV-45's
> `3W + 1`, never run. A **streaming** relay carries 128-word frames through real
> cells exactly. The bound is real only for the *hold-all-in-registers* relay, and
> for that shape it is **W ≤ 9**, not 10.

### The three walls, re-measured

| wall | verdict |
|---|---|
| 1. capacity — 17 cells/QR × 80 = 1360 vs 120 | **STANDS.** Not re-derived. Forces REUSE. |
| 2. the 32-word state cannot transit a cell | **FALSE.** See above. Layer: a property of a CODE SHAPE — not hardware, not toolchain. |
| 3. HOP_CNT/DEST are immediates | **STANDS as an ISA fact, but does not bind this block.** |

Wall 3 does not bind because **the permutation is not data-dependent**. All 80
invocations are ten identical repeats of a fixed 8-step cycle (only 8 distinct
quadruples exist in the whole cipher). Better: **every quadruple takes exactly one
word from each of the four rows** — so with row `k` in its own lane cell, the `4k`
term of `index(k) = 4k + ((j + k*shift) & 3)` is *which cell* (static routing), and
the only computed value is the 2-bit within-row selector, i.e. a `LOAD [Rn]` index.
**No SRAM panel is needed** — which deletes the entire integration surface the
previous attempt died on (`run_block_dut` has no panel awareness, push-read
descriptors, `resolved_io` landing-cell ordering).

### Measured on chip this session (all on the real 10×12)

1. **Recirculation.** A backward `JUMP` re-entering a cell mid-program with the
   counter in cell state: **1/2/4/8/10/20/80 passes over ONE datapath, exact every
   time.** This is what makes wall 1 survivable.
2. **The lane cell.** `LOAD`-indirect read AND 4-way-`CMP`/`BR` write-back (the ISA
   has **no `STORE [Rn]`**) correct for **all 32 (row, half, selector)
   combinations**, carrying the real RFC §2.3.2 state, exactly one slot changed per
   write.
3. **The sequencer.** `seq_ctl` + `seq_sel` emit **all 80 invocations of the RFC
   schedule exactly**.
4. **QR reuse.** The shipped 17-cell `ChaCha20QRBlock` sustains **all 80 sequential
   invocations bit-exact** (640 words in, 640 out), and the loop + add-back
   reproduces the RFC §2.3.2 output state.

### Architecture (register-resident, panel-free, 37 cells of 120)

8 lane cells + `seq_ctl` + `seq_sel` + the 17-cell QR engine **reused verbatim** + an
8-cell write-back peel chain + finalizer + serializer. Every cell authored and
budget-checked and every one FITS: lane 20 instr / base 11 / max pin 10; `seq_sel`
19/12/6; `seq_ctl` 20/11/9; peel 7/24/3; final 9/22/4; ser 6/25/2.

Two design facts worth reusing:

* **A single 8-way demux does not fit** (~48 instructions vs a 31-word budget); a
  chain of eight one-slot cells is ~7 each. **Cells are the surplus resource, words
  are the scarce one** — splitting a cell is nearly always the right answer to an
  overrun.
* **The 32-bit add-back must live in ONE cell.** ALU flags are per-cell, so a carry
  cannot cross a cell boundary.

### The gotcha that would have been invisible

**The counter directions are load-bearing.** The COLUMN half must run first and `j`
must **ASCEND**. A descending `j` gives `j & 3 = 0,3,2,1` and silently computes a
**different cipher** — while still producing exactly 80 invocations, so no
structural or count-based check catches it. Only a schedule-value gate sees it.
This is the same class as the QR block's free-`rot16` mutant. Now gated.

### What is NOT done

The cells are authored and individually proven but **not wired** — no
`build_cell_programs` / `internal_connections` / `internal_jumps` /
`default_layout`, so the assembled block does not compute, and **its source is
deliberately not committed**. Shipping a non-computing block source was the exact
fault of the previous attempt; a block that does not compute is not a block.

**Next agent:** wire the 37 cells, fold them (INV-8/9/14), gate against RFC 8439
§2.3.2 and §2.4.2. Start from `test_chacha20_keystream_golden.py` (23 tests, now
pinning the schedule-is-a-constant properties and the counter-direction mutants)
and `test_wide_transit_ceiling.py` (38 tests, the recirculation gate). See INV-49.

### Method note

Every "cannot" in the previous quarantine that was *derived* turned out to be
wrong, and every one that was *measured* held. The 20 minutes it took to build a
streaming relay and run it would have prevented a quarantine that then became a
law in `invariants.md` and was cited to size a future architecture.

## LZ4DecoderBlock — QUARANTINED on the panel-template cell cap; the decoder itself is proven on chip 2026-08-29

> **SUPERSEDED — read the entry above.** Kept because its wrong claims were copied
> into INV-48, the manifest and the factory record, and a reader needs to recognise
> them. The cell cap does not exist; the "no single-face ring" search asked the
> wrong question (the fabric is not a ring, and a cell's targets need only lie
> along its one outgoing *walk*); and the "template rejects this block" test
> asserted merely that something raised. The DSP findings below are all still good.

Decodes the published **LZ4 block format** — `[token][literal length][literals]
[offset][match length]` sequences — into the original byte stream, with the history
window in the SRAM panel. **Outcome: `needs_human`.** The golden and the cell programs
are verified (38 tests, `verification/tests/test_lz4_decoder.py`); what is blocked is
PLACEMENT, and the wall is sharp enough to be actionable. Promoted to **INV-48**.

### What IS proven (so the next agent does not redo it)

* **The golden is validated against the reference C implementation**, not just against
  itself: `verification/tests/lz4_golden.py` decodes blocks produced by the reference
  LZ4 compressor byte-for-byte on 6 payloads, and the reference C DECODER accepts the
  blocks this suite manufactures. That is the check that stops a golden and a block
  being self-consistently wrong together.
* **The cell programs run correctly on a real chip.** The token nibble split (all five
  token classes), the little-endian offset assembly (4 byte pairs), and — the decisive
  one — a whole **match copy driven through a real `SramPanelDevice` + `PanelDriver`**,
  with each byte push-read at the *computed* address `wpos - off` and appended to the
  window at `wpos` before the next fetch. The `offset == 1` byte run is exact on chip:
  only `window[0]` is preloaded and the remaining six bytes are ones the same match
  produced a moment earlier. That is the classic decoder bug, closed on silicon.
* **The panel cost, measured:** a history write is 3 panel-port words (`set_addr` then
  `write` = `WRITE R5`, `WRITE R2`, `JUMP R0`); a push-read is 6 (`WRITE R3/R4/R5`,
  `JUMP R1`, plus the 2-word panel-originated return). So **9 panel-port words per
  back-reference byte** against 3 per literal byte, all sequential on a
  single-outstanding held-ack link. That is the number that scopes the encoder.

### The wall, in three parts

1. **`auto_pnr` never routes a panel design generically.** It branches on
   `panel_backed_blocks(...)` and hands the whole design to
   `engine/panel_pnr.apply_panel_template`, then runs `auto_route_all` only for the
   leftovers. The CP-SAT pack + perturbation sweep is never reached.
2. **The templates place only the NAMED ROLE cells.** TX shape: controller at
   `x1_out`, consumer at `(0,1)`. RX shape: controller, kicker, input, consumer —
   and it writes VaricodeDecoder-specific params (`read_addr_hop`/`read_dest`/
   `read_entry`) into the block, so a block without them dies in its own
   constructor. **CORRECTED 2026-08-29:** this bullet previously read "every
   shipped panel block is 2-3 cells, so the cap had never bound" — that was FALSE
   and there is no cell cap. `GolayDecoderBlock` is **7 cells, panel-backed,
   `done`, BER 0**, shipped 2026-08-16 — before this entry was written. What
   actually binds is that a cell with no named role gets no position, so it is
   silently left out of the `Placement`.
3. **This FSM cannot be 3 cells, and cannot be a ring.** The parse+emit datapath is
   **102 instructions**; the real per-cell budget is `31 - (data + state + inputs)`,
   at best 28 and realistically 25-26 — a **4-cell absolute lower bound**, 6 as
   actually decomposed, plus the controller. And an exhaustive search finds **no
   single-face ring ordering** of the 7 cells at any ring size 7..16 that avoids
   transiting the controller (which sits at the port with its face pointing off-chip):
   the emit cell has an edge to the controller AND an edge back to the FSM head, and
   in a ring one must wrap past the other.

### Three encodings worth stealing (they are what got 6 cells to fit at all)

* **Hold a counter NEGATIVE to mark a sub-phase.** `lit` is stored as `-(15+sum)` while
  inside the literal-length continuation, so the phase test is the sign bit of a value
  the cell already has (one `SUB` sets `N`). That deleted an entire `ext` register and
  its `CMP` from the hottest cell.
* **Apply a constant offset ONCE, at the token, and let it become the sentinel.** `mat`
  is seeded as `nibble + MINMATCH`, so "was the nibble 15?" is a single compare against
  19 rather than a carried flag.
* **Let one counter be the loop counter AND the discriminator.** The emit cell serves
  both a literal byte and a match byte with one program body: `emit_lit` zeroes `mat`,
  so the shared `SUB mat, one` goes to `-1` (`N` → stop) for a literal, `0` (`Z` →
  finish the sequence) on a match's last byte, and positive (→ fetch the next). That
  removed a whole `inmatch` register and its two setup instructions — and, because it
  put the copy loop entirely inside one cell, removed a cross-cell back-trigger from
  the layout problem too. **Loop back-edges are the expensive ones.**

Also confirmed while doing this: the shipped panel topology is **panel on the x1 pair,
data on x16** (`engine/sram_demo.py`, `engine/panel_pnr.py`, `psk31_transceiver`) — the
opposite of what one might assume, and it has to be that way round because a data word
is 16 bits and the x1 port is one bit wide.
## ChaCha20KeystreamBlock — QUARANTINED: the transport ceiling forces a resident state, and the permutation becomes ADDRESS ARITHMETIC (proven on chip) 2026-08-29

**Result: QUARANTINE (`needs_human`), with the architecture measured and its
load-bearing mechanism proven on the real placed+routed chip.** The block source
is NOT committed — it builds and routes but does not yet compute. The validated
golden and the on-chip proof of the key mechanism ARE committed, so the next
builder starts from evidence rather than from scratch.

**The scoping, which is the point of the entry.** Three independent walls, all
arithmetic, all measured — none of them the ALU:

1. **The unroll is 10x over.** 20 rounds x 4 quarter rounds = **80** invocations;
   `ChaCha20QRBlock` measured **17 cells**; `17 x 80 = 1360` against a **120**-cell
   array. The quarter round must be REUSED, not instantiated.
2. **The state cannot transit a cell at all.** INV-45 prices a `W`-word relay at
   `3W + 1` of the 31 usable words. The ChaCha state is 16 x 32-bit = **32
   sixteen-bit words**, so `3*32 + 1 = 97` — more than three whole cells for ONE
   hop. Solving `3W + 1 <= 31` caps a transiting live set at **W = 10**; the
   quarter round's 8-word frame already leaves only 6 words of body. So the state
   must SIT, not move. This is the sharpest form of INV-45 measured so far.
3. **The permutation cannot be ROUTING.** Column and diagonal rounds read
   different index quadruples, so "which state word feeds this quarter round" is
   data-dependent — but a `WRITE`'s `HOP_CNT`/`DEST` are INSTRUCTION fields
   (guide §4) and there is no cross-cell register addressing. **A cell cannot
   compute where to send a word.**

**The escape hatch, and the durable finding.** The SRAM panel (INV-31) solves 2
and 3 *at the same time*, because a panel ADDRESS is a DATA word: the state
becomes resident and addressed rather than carried, and the permutation becomes
computed addresses instead of computed routing. Concretely, the RFC states the
schedule as eight literal quadruples, but both halves collapse into one closed
form:

    index(k) = 4*k + ((j + k*shift) & 3)        shift = 1 if diagonal else 0

Three instructions (`SHL #2` / `ADD` / `AND #3`) and **no lookup table**. That
identity is what makes the block expressible at all, and it is gated in
`test_chacha20_keystream_golden.py` against the RFC's literal quadruples.

**Proven ON CHIP (not modelled).** A built, placed and routed `gather` cell,
driven through the real simulator with `j`/`shift` preset, emitted the exact RFC
panel-address sequence for **all 8 quadruples** — both halves, including the
wrap-around diagonals (`(1,6,11,12)`, `(2,7,8,13)`, `(3,4,9,14)`). The whole
27-cell design folds 6x5, every cell inside its 31-word budget, `auto_route_all`
ok and `build` ok on the 10x12. So the architecture is real; what remains is
integration.

**Why it is not done.** The assembled 80-invocation loop emits ZERO words and
commits ZERO panel writes. The remaining work is harness/integration, not
architecture: (a) `run_block_dut` has **no panel awareness whatsoever** — zero
`panel`/`sram` references in `dut_runner.py` — so a panel block needs a bespoke
harness (mirror `test_cw_keyer_sram.py`), which is how every shipped SRAM-backed
block is gated; (b) the push-read descriptors are placement-derived and were
still `0` (the disabled sentinel), so reads went nowhere; (c) the landing-cell
selection interacts badly with a two-entry sequencer — `engine/catalog.py`'s
`resolved_io` picks **the first cell that declares inputs**, so cell ORDER in
`build_cell_programs` decides where the external trigger lands. Ordering `desc`
before `seq` silently started the loop mid-stage with the schedule registers
uninitialised, and the design still built and routed clean.

**The recurring trap, worth its own gate.** FOUR separate times a cell went over
the 31-word budget and the design STILL assembled, placed, routed and built
clean — the silent INV-45/INV-33 failure, every time looking like a routing
fault. A static per-cell assertion — *no pinned register, state var or data
address may be `>= 31 - instruction_count`* — caught every one immediately. Any
multi-word block should carry that gate from the first commit, not after the
first wrong answer. (Watch the direction of the trade, too: converting a stored
`zero` constant into `SUB Rx, Rx` frees a register but costs instructions, and on
an already-tight cell that made the overrun *worse*, not better.)

**For the next builder.** Start from `verification/tests/chacha20_golden.py`
(now exact against §2.3.2 state, §2.3.2 keystream and §2.4.2 encryption) and the
on-chip address-schedule proof. Build the **bespoke panel harness first** and get
a single quarter round to read 8 words, compute, and store 8 words back through
the real panel; only then close the 80-iteration loop. Drive the panel directly
from an authored cell (`WRITE @ph,N` / `JUMP @ph,N`, the `CWKeyerBlock` fetch-cell
pattern) rather than through `SramControllerBlock` — the generic controller
carries ONE fixed descriptor pair and its own auto-incrementing address, and a
quarter round needs eight COMPUTED addresses. Keep the address/payload/commit
triple inside ONE cell: split across two, it is a reconvergent fan-in (INV-20)
whose arms can interleave and store a word at the wrong address.

**Unrelated pre-existing bug found in passing:** `test_pipeline_saturation.py`
has a DUPLICATE `CWKeyerBlock` key (lines 615/616); the second, stale
`QUARANTINE (INV-29)` string wins the dict merge and is now factually wrong (the
block is SRAM-backed and verified). Identical to the `VaricodeDecoderBlock`
duplicate that was fixed at lines 592-593; the same removal was never done here.
## XorJoinBlock — the N=2 rendezvous at its cheapest, and a mutation test that proved nothing 2026-08-29

`out = a ^ b` for two INDEPENDENT producers. **Outcome: `done`**, 59 tests, EXACT
tolerance 0, on the real placed + routed + built chip. One cell, built directly from
`FeaturePairJoinBlock`'s N=2 LOCK-by-face rendezvous with the two-burst emit replaced
by one native LOGIC `XOR` + a single brokered write. It went in essentially first-try;
everything below is what the *verification* turned up, which was the interesting part.

**Why the block exists at all.** `XorBlock` already computes this function and is
gated bit-exact against `blocks.xor_bb`, but its operands arrive via the complex-burst
fan-in, which keys on `(src_cell, in_cell)` — both words must come from ONE source
cell. The stream cipher needs plaintext and keystream from two SEPARATE chains. So the
distinguishing mechanism is not arithmetic, it is topological: arrival FACE + the
arbiter LOCK (INV-46 at N=2).

**INV-19 passed, and the reason is structural rather than lucky** — worth stating
because the N=3 voter's saturated gate found a real deadlock and it would be easy to
assume this family is generally hazardous. The face budget is `N + 2` (arms + forward
+ release corridor); at N=2 that is 4 and a cell has 4, so the whole rendezvous fits
in ONE cell — and a single cell needs neither a forward nor a release, because there
is no internal datapath for queued samples to pile into. The LOCK it already carries
IS the serialization INV-19 prescribes. Whole-burst `queue_words_physical` with no
quiescence anywhere equals the per-sample drive, values and 1:1 count.

**THE REAL LESSON — a mutation that is a NO-OP certifies nothing.** The spec's third
named mutation was "emit before latching the second operand", and the obvious way to
write it is to swap `MOVE R0, R{in:b}` with the `XOR`. That mutant BUILT, RAN, and
emitted the CORRECT golden stream. The cause: both input ports are declared at R0
(each arrives on its own face-gated trigger — the shipped N=2 convention), so that
MOVE assembles to `MOVE R0, R0` and reordering it changes nothing. Had that been
written as the INV-4 gate and observed to "fail as expected" on a modelled stream, the
suite would have carried a mutation that could never fire. The fix was to mutate the
REAL block and rebuild on the REAL chip: `_SUBSTRATE_MUTANTS` corrupts the assembly,
rebuilds, and asserts the output diverges. Measured — drop the XOR → forwards `b`;
AND instead of XOR → `a & b`; drop the `a` latch → forwards `b`; drop the re-lock →
2 words then desync. The redundant MOVE is KEPT (one word of 32) because it makes the
operand explicit rather than dependent on a register-allocation coincidence, and the
fact is pinned by a test so nobody removes it without knowing what it does and does
not protect.

**A SECOND, SHARPER TRAP: a broken block collapses this suite into SKIPS, not
failures.** Per INV-46 Rule 4 the harness smoke-probes each candidate layout and moves
to the next anchor on failure, ending in `pytest.skip` if none survive. That is right
for a flaky CP-SAT run and dangerous for a broken block: corrupting the XOR to an AND
turned 35 tests into skips and only 6 into failures — a suite that still reads
"passed" at a glance. `test_the_probing_harness_actually_routes_this_block` now FAILS
(never skips) if no anchor yields a correctly-pairing chain, so a wholesale collapse
into skips cannot be mistaken for green. **Any face-locking block's suite wants this
guard**; the probing pattern the family requires creates the hazard.

**Stimulus design is load-bearing for XOR specifically.** A mis-paired XOR emits a
plausible-looking byte, not an obvious failure, so every gate here uses values whose
per-sample XORs are all distinct AND whose cross-sample XORs are disjoint from the
correct ones — asserted in the test, not assumed. The first draft of the smoke probe
used `0xAA`/`0x55`, which XOR to `0xFF` whichever way they pair; the back-to-back gate
caught its own lazy constants via that same non-vacuity assertion. Symmetric stimulus
cannot see a rendezvous bug.

**Golden.** No stock GR counterpart for the two-independent-producer case, but the
FUNCTION is stock, so the golden is cross-checked three ways: against a LIVE
`blocks.xor_bb` over 512 random byte pairs, against the shipped `XorBlock`'s
reference, and pinned independently so it still gates without GNU Radio installed.

**Also verified:** self-inverse `(x ^ k) ^ k == x` with BOTH halves computed on chip
(with a non-vacuity check that the ciphertext really differs from the plaintext);
commutativity across the two arms; unequal producer rates (n=2 vs n=4) still pair
emission-for-emission; all 8 D4 orientations; the same-face DRC; and the marker
imports under the real GR interpreter with 1-byte io_signature matching the yml's
`byte` dtype.

## TMRVoterBlock — the LOCK-rotation rendezvous generalises to N=3, and the FACE BUDGET is what decides how far it goes 2026-08-29

Three redundant chains converge on one block from three DISTINCT faces; the block
votes and emits a 2-word `[value, status]` packet. **Outcome: `done`**, 54 tests,
EXACT tolerance 0, verified on the real placed + routed + built chip: all five vote
cases, all 6 relative arrival orders, random interleavings over 3 seeds, all 8 D4
orientations, and every INV-4 mutation proven to fail. It is the N=3 member of the
family `DualFloatToComplexBlock` / `FeaturePairJoinBlock` established at N=2.

### The durable finding: N=2 was baked into the engine in three separate places

The mechanism is generic over N. The code serving it was not, and **none of the three
failures announced themselves** — each produced a layout that built and routed
cleanly and then misbehaved for a reason that looked unrelated:

1. **`cpsat_placer.py` — the distinct-face constraint constrained only the FIRST TWO
   drivers** (`d0, d1 = drvs[0], drvs[1]`) **and skipped multi-cell blocks outright**
   (`if len(pl.cells) != 1: continue`). So the third arm was unconstrained and the
   whole block was exempt. Symptom: the build DRC rejected the layout with
   `dual_input_same_face` — a correct complaint pointing at the wrong culprit.
   Fixed: constrain ALL drivers, drop the single-cell gate. The consumer term must be
   SKIPPED for a multi-cell block: N drivers + a consumer is satisfiable only for
   N ≤ 3, and a multi-cell block's consumer is fed from a DIFFERENT cell so it
   contends for no face at the rendezvous — adding it makes the model infeasible and
   loses the whole placement.
2. **`bus_router.py` — broker REUSE is the same-face bug.** The router's ordinary
   fan-in behaviour is that a second net into the same target cell reuses the broker
   already serving it (`_broker_abutting`). For a face-locking target that is
   precisely how two arms end up on one face. Measured: all three arms funnelled
   through ONE broker cell, all arriving WEST. Fixed by `_distinct_face_target_cells`
   — reuse disabled for these targets, and each net's broker candidates filtered
   against the faces siblings have already claimed. (The MAZE router already did this
   correctly and generically; the bus router, which runs first and succeeds, did not.)
3. **`build.py` — the backward `unlock` edge hardcoded CONFIG 4 (LOCK).** This block's
   interlock RE-POINTS a rotating face lock (CONFIG 3 = LOCK_FACE) rather than
   clearing the arbiter lock. The pass silently rewrote the authored
   `WRITE.CFG @N, 3` into a lock-CLEAR, which un-gates every face and lets out-of-turn
   arms barge in: it built, routed, voted correctly for two samples, then desynced.
   Fixed by a block-declared `UNLOCK_CFG_ADDR` (default 4, so every existing block is
   byte-identical).

Also added to `controller.py`: the same-face verdict is now computed IN the
router-selection loop (`_rendezvous_input_same_face`), so a bus-router report with a
same-face landing is treated as INVALID and escalated to the maze router — instead of
surfacing much later as an unexplained build error.

### The FACE BUDGET — the arithmetic that decides the whole design

    a cell has 4 faces; an N-arm rendezvous needs
      N (one per arm — the face IS the path identity)
    + 1 (forward into the block's datapath)
    + 1 (a serialize-LOCK release corridor coming back)
    = N + 2

* **N=2**: 4 faces. Fits — and the shipped N=2 blocks are SINGLE-CELL, so they need
  neither a forward nor a release, which is why the budget never came up before.
* **N=3**: FIVE needed, four available.

Everything about this block's shape follows from that one line:

* The rendezvous must be a **LEAF of the fold** (exactly one in-block neighbour). A
  compact 2×2 square — the obvious 4-cell fold, and the first tried — gives every
  cell two in-block neighbours, leaving two free faces for three arms; the maze
  router reports *"no free DISTINCT-face broker for a face-locking block's input"*
  and the chain does not route at all.
* The block is a **colinear 4×1 chain** (rendezvous → agree → disagree → emit), the
  longitudinal shape `layout_rules` warns against, because the face budget forces it.
  It is affordable here: 4 ≤ 8 across, and the three inputs land on one cell from
  three sides rather than tapping a bus edge, so the co-located-I/O convention does
  not apply.
* The serialize-LOCK release **cannot have a corridor of its own** and must come back
  through the one abutting cell, `agree` — the FIRST stage of a three-stage chain.

### INV-19 found a real construction bug, and then a real wall

The obvious construction re-locks straight to `face_a` at the end of `got_c`. That is
correct per-sample and **deadlocks under saturation**: it re-admits the next sample's
first arm the instant the current triple is dispatched, triples pile into the chain,
and the simulator reports an explicit `Deadlock` after exactly ONE packet. (The three
producer arms driven saturated WITHOUT the voter are fine, so the hazard was the
block's — worth measuring before blaming the harness.) The fix is INV-19/20's own
idiom: **the rotation has FOUR stops, not three** — `got_c` locks to the INTERNAL
FORWARD face, which no external arm ever arrives on and therefore bars all three, and
`agree` re-points `LOCK_FACE` at arm A once it has dispatched.

The RESIDUAL limit is the face budget again, and it is guarded, not waived
(`test_known_limit_saturated_burst_depth_is_one`): **one triple in flight**. Arms may
arrive in any order WITHIN a sample (all 6 permutations verified), and any number of
triples may be driven one-at-a-time; but two complete triples queued before running
deadlock, and an arm that runs a whole sample ahead truncates. Three deeper release
points were built and measured, all blocked — (a) a backward `WRITE.CFG` transiting
the datapath row is re-forwarded by the live cells' committed faces and lands on a
real entry (it fired `nomaj` and emitted a spurious `[sentinel, 7]`); (b) a dedicated
`transit_*` unlock lane must enter the rendezvous on a face, and all four are
committed, so the DRC rejects it; (c) a backward JUMP into a relay entry is rewritten
by the exit-default to the emitting cell's own entry.

### Two smaller things worth keeping

* **The algebraic identity that made the tree fit.** If `a != b` then the majority,
  whenever one exists, is ALWAYS `c` — a majority needs `c` to equal one of them. So
  the disagree half needs no value selection at all. Written naively the tree stages a
  selected value in a state register and does not fit any cell; with the identity it
  does. (Trading a data word for an instruction is always neutral on this ISA — 32
  words is 32 words — so the only real savings are structural.)
* **A layout probe belongs in any harness for a face-locking block.** `auto_pnr` is a
  CP-SAT search and is not deterministic: ~4% of layouts that route, build, and
  present three distinct landings still mis-deliver an arm. That surfaced as a ~50%
  per-run flake spread across whichever gate happened to draw a bad layout —
  indistinguishable from a real block bug, and exactly how a flake hides one. The
  harness now smoke-tests each candidate layout on a THROWAWAY chip instance (a
  healthy triple plus one single-fault triple per arm) and moves to the next anchor if
  it fails. A healthy-only probe is NOT enough: a mis-delivered arm still votes 0 when
  all three carry the same value. Use a throwaway chip — driving a triple advances the
  lock rotation and latches arm state, so smoking the chip a gate is about to use leaks
  the probe's values into that gate's first vote (measured: a phantom "arm A faulted").

Golden: `verification/tests/tmr_golden.py` (written from the spec, cross-checked
against the block's own `vote` over the whole interesting domain). Saturated coverage
is BESPOKE — no shared harness can drive three independent producers on three distinct
faces.
## ChaCha20QRBlock — 32-bit arithmetic on a 16-bit ALU: CARRYING a wide value costs 4x what COMPUTING on it does 2026-08-29

**Outcome: `done`.** One ChaCha20 quarter round (RFC 8439 §2.1), 17 cells, exact on
the real placed-and-routed chip: the RFC's §2.1.1 quarter-round vector AND its §2.2.1
`QUARTERROUND(2,7,8,13)` state vector, 12 random 32-bit frames over 3 seeds, and 7
wrapping corners — all bit-exact, tolerance 0, in all 8 D4 orientations, saturated ==
per-sample. No GNU Radio counterpart; the golden is the published algorithm pinned by
the RFC's own two independent vectors before it is allowed to gate anything.

### The measured instruction costs — these REPLACE the plan's estimates

Measured, not estimated (counted in the built cell programs; the whole-block totals
are asserted in the test's report):

| 32-bit op on 16-bit halves | instructions | how |
|---|---|---|
| `ADD` (mod 2^32) | **4** | `ADD lo,lo / MOVE lo,R0 / ADC hi,hi / MOVE hi,R0` |
| `XOR` | **4** | two 16-bit `XOR`s + their parks |
| `ROTL32(x, 16)` | **0** | the hi/lo swap, folded into the relay (below) |
| `ROTL32(x, 12)` | **7** over 2 cells | 4 (`ROL` both halves) + 3 (masked merge) |
| `ROTL32(x, 8)` | **7** over 2 cells | same |
| `ROTL32(x, 7)` | **7** over 2 cells | same |

Counted as body instructions ABOVE the 17-instruction transport baseline that every
relay stage pays (8 words x `MOVE`+`WRITE`, plus the trigger `JUMP`). The `rotb` merge
is 5 instructions of which 2 ARE the relay writes it replaces, hence 3 net.

Whole quarter round: **53 instructions of actual arithmetic** — under the plan's ~60
guess. But the block is 17 cells, not the 3-5 the plan budgeted, and the reason is the
finding below.

### `ADC` needs no help: MOVE is flag-preserving

`ADD lo / MOVE / ADC hi / MOVE` is a correct 32-bit add because only ALU ops touch the
flags — the park `MOVE` between the `ADD` and the `ADC` does not disturb the carry.
The carry is never synthesised, never re-derived with a `CMP`. (CostasLoop's
`int_lo/int_hi` accumulator was already doing this; it is now the named idiom.)

### `ROTL32(x, 16)` is FREE — and "free" means zero instructions, not two MOVEs

A rotate by exactly the half-width is the hi/lo swap. The cheap implementation is two
`MOVE`s; the FREE one is to not move anything at all and instead swap **which register
each relay `MOVE` reads**. Every stage already re-reads all eight frame words to
forward them, so the rotate rides on work the cell was doing anyway. Cost: 0.

### `ROTL32(x, n)` for n < 16, WITHOUT a cross-half shift

The obvious form needs both original halves alive while writing both results:

    hi' = (hi << n) | (lo >> (16-n));   lo' = (lo << n) | (hi >> (16-n))

which is 11 instructions and 2 scratch registers — over budget for a relay cell. The
cheaper identity uses the 16-bit **rotate** (`ROL`, the `ROT` bit of `SHL`) on each
half independently. With `u = ROL16(hi, n)`, `v = ROL16(lo, n)` and `M = (1<<n)-1`:

    hi' = u ^ ((u ^ v) & M)          lo' = v ^ ((u ^ v) & M)

`ROL16(hi, n)` already holds `hi << n` in its high bits and `hi >> (16-n)` in its low
`n` bits — exactly the two pieces the cross-half form assembles — so all that remains
is to TRADE the low `n` bits between the halves, which is one shared `k = (u^v) & M`
and two XORs. It splits into a 4-instruction cell and a 5-instruction cell, both of
which fit alongside the 8-word relay. Verified over the full 32-bit domain for
n = 12/8/7 before any silicon.

### THE finding: transport dominates, 4:1

A quarter round's live set is all four 32-bit words — none dies early, all four are
outputs — so **every inter-stage hop carries the whole 8-word frame**, at
`MOVE R0, Rw` + `WRITE` = 2 instructions per word:

    8 held words + 16 relay + 1 jump = 25 of the 31 usable words

leaving **6** for the stage's own data + state + body. That is the binding constraint,
and it is why the block is 17 cells for 59 instructions of arithmetic: the ADD (4) and
XOR (4) fit, the 11-instruction rotate does not, so the rotate had to be reshaped
until it split into two cells that each fit. **Relaying the 8-word frame one hop costs
17 instructions; the arithmetic done on it at that hop costs 3-4.** Wide-value
dataflow, not the ALU, is what sizes a multi-word block on this substrate. Promoted to
**INV-45**.

### The bug that cost the most: an over-budget cell is SILENT and looks like a routing fault

The egress cell holds the finished frame and bursts it out as eight `WRITE`+`JUMP`
pairs on one port (the `UpsamplerBlock` rate-expanding idiom). Eight words is exactly
**one word over budget**: 8 inputs + 8x(MOVE/WRITE/JUMP) = 24 instructions puts
`base_addr` at 31-24 = 7, so the frame's own R7/R8 sit **on top of the cell's first two
instruction words**.

The resolver does not catch this. Its space guard compares only DATA against
`base_addr` — never state, never pinned inputs (INV-33's overlap half). So the cell
assembled, the bitstream loaded, the block placed and routed cleanly, and the burst
came out **seven words long, missing its LEADING word, with the other seven bit-exact**.
Dumping the egress cell's registers after the run showed all eight words present and
correct; the compute pipeline was never wrong. Symptoms that misled:

* it looked like a first-word-of-burst handshake artifact, so the first hypothesis was
  the harness drain. A hand-rolled driver that drains aggressively between every run
  step got the same 7 words — **the loss was real, on-chip, not a drain artifact**.
* `UpsamplerBlock` at `sps=8` emits all 8 words correctly, which ruled out "burst
  egress drops a word" as a substrate property and pointed back at this cell.
* adding a dummy leading write to test the theory made it WORSE (still 7 words, now
  with a garbage word) — because the extra instruction pushed the overlap further, not
  because the theory was wrong. A mutation that makes an over-budget cell worse is not
  evidence about the mechanism you were testing.

**The fix, and it is the reusable one:** deliver frame slot 0 into the egress cell's
**R0** (INV-33's accumulator-delivery idiom) as the upstream stage's LAST write, and
make that word's `WRITE` the cell's FIRST instruction — no `MOVE` needed, and nothing
has run yet to clobber R0. That saves the one instruction AND the one register the cell
was over by: 23 instructions, `base_addr` 8, frame at R1..R7, clean. It requires the
upstream relay tail to write that slot **last** (a `last=` ordering hook), because any
later write would disturb the delivered R0.

`test_chacha20_qr.py::test_no_cell_overlaps_its_own_instructions` is now a static gate
over every cell of the block, paired (INV-4) with
`test_overlap_gate_catches_the_known_bad_shape`, which re-inflates the pre-fix 8-word
egress cell and asserts the gate FAILS on it.

### A `__terminate__` jump on the external output port DELETES that port

`internal_jumps()` returning `("emit", "out", "__terminate__", "default")` puts
`("emit","out")` into the portmap's `internal_srcs`, and the portmap then excludes it
as internally-consumed — leaving the block with an input port and **no output port at
all**, `io_colocated=False`. The block still built and ran correctly through an
explicitly-wired logical connection, so the DUT gate was green while auto-placement saw
a portless block. The egress `WRITE`/`JUMP` pairs ARE the external handshake
(`UpsamplerBlock` declares no terminate edge); do not also declare them internal.

### Two constants at R0 (twice)

Both the frame counter's `one` and every relay stage's mask were first allocated at
address 0. `SUB R{n}, R{one}` reads R0 and, being an ALU op, WRITES R0 — so the
constant survives exactly one instruction and the counter free-runs after the first
sample. All data now starts at R1; the R0 slot is a deliberate hole. This is INV-33
verbatim and it still cost a debug cycle, which is the argument for the static gate.

### Interface and shape

8 words in / 8 words out, one word per trigger, the result frame bursting on the
eighth — so `run_block_dut_rate` (which drains every word per trigger) is the driver;
`run_block_dut` keeps only the last word and cannot see a burst. The serial word
stream is turned into a resident 8-word frame by **two 4-deep shift-register collector
cells**: `in0` (the landing cell) spills its oldest word to `in1` and re-publishes its
four held words every trigger unconditionally, and `in1` holds the mod-8 counter and
fires the compute head once per frame. Making `in0` unconditional — no branch, no
second jump, no lock — was deliberate: the intermediate publications are simply
overwritten because the head is only ever triggered by `in1`.

Fold: 8x3 out-and-back serpentine, `in0` at (0,0) and the egress cell at (0,2), both on
the west edge (`io_colocated=True`).

### Scoping the rest of the wave — READ THIS BEFORE ChaCha20KeystreamBlock

**17 cells per quarter round is real and measured.** A full ChaCha20 block is 8
quarter rounds per double-round x 10 double-rounds = 80 quarter rounds. Unrolled that
is 1360 cells against a 120-cell array — **off by more than 10x**. A keystream block
CANNOT be a straight-line expansion of this one. It must REUSE a small number of
quarter-round instances across rounds, which means a feedback/state-machine topology
(round counter, state permutation between rounds) that this block deliberately does
not have. Note also that transport, not arithmetic, is the cost: the 16-word ChaCha
state is 32 16-bit words, which no single cell can hold, so the state itself has to
live distributed and the round permutation becomes a routing problem. Budget the next
dispatch for that, not for "QR x 8".

For Poly1305 the ADD/ADC idiom, the free half-width rotate, and the rotate-then-merge
identity all carry over directly; the transport ceiling (6 words of body per relay
stage holding an 8-word live set) is the number to design against.

### The mutation gates were run on SILICON, not just on the model

Model-level mutants prove the GOLDEN discriminates; they do not prove the chip gate
does. Three mutants were therefore built, placed, routed and RUN on simKYT:

* `rot7` built as `rot6` (a perturbed rotate constant),
* `ADC` replaced by `ADD` on the high half (the dropped carry),
* the free `ROTL32(d,16)` hi/lo swap removed.

All three were caught, and — the part that matters — **each still emitted the right
NUMBER of words (24/24)**, so they failed on VALUES alone, not on a broken build. The
rot16 case is the one worth keeping: because that rotate costs zero instructions, an
incorrect one is invisible to every size, budget and word-count check that exists. Only
a value gate can see it. They ship as
`test_onchip_mutant_{perturbed_rotate_constant,dropped_carry,removed_rot16_swap}_fails`.

### Anchor sweep, because an 8x3 fold is big enough for it to matter

The AGCCC / ComplexToMag precedent is that corridor disjointness is
ANCHOR-DEPENDENT for large folds — a block computes correctly at one placement and
routes its corridor through the port cell at another. This fold is 8x3, so the RFC
vector is gated from all 8 anchors it fits, not just the harness default (1,1). All 8
exact; no anchor-dependent fragility here, but the gate is cheap and the hazard is
real for the bigger blocks this family will grow into.

### The full suite is RED at this commit, and none of it is this block

Recorded so the next builder does not mistake a pre-existing red suite for their own
regression. A full `verification/tests/` run at this commit reports ~68 failures, of
which ~57 are CASCADE: the INV-38 session guard makes every `test_emit_report` /
`test_write_report` refuse to write once ANY gate has failed in the session, and those
writers then fail themselves. The real failures are ~11, and **ChaCha20QRBlock appears
in none of them** (0 occurrences of "chacha" anywhere in the failure list).

Six were reproduced IDENTICALLY at the parent commit `8db6e49` in the untouched main
checkout, so they are pre-existing:

* `test_route_quality[fft128_2p2s]`, `test_route_quality[fft_spectrum_32]`
* `test_iir_biquad::test_iir_matches_gnuradio_production_range[0.1]`
* `test_qam16_costas_report::test_qam16_costas_chain_ber_zero_and_report`
* `test_fec_link_example::test_shipped_grc_user_path`
* `test_examples_grc_userpath::test_fft128_2p2s_shipped_grc_user_path`

(`test_route_quality` + `test_iir_biquad` give exactly `3 failed, 53 passed, 1 skipped`
on BOTH trees — same tests, same counts.)

**A failed full run DELETES report JSONs, so never `git add -A` after one.** INV-38's
"absence is the safe state" means each writer UNLINKS its report before the verdict is
known; when the session then fails, ~57 reports stay deleted on disk. A reflexive
`git add -A && git commit` after such a run stages all of those deletions — I did
exactly that here and had to `reset --soft HEAD~1` + `git checkout -- verification/
reports/` to undo it. After any failed suite run, restore the reports before
committing, and stage the files you actually edited BY NAME.

The remaining `*_shipped_grc_user_path` failures (complex_math, lms_equalizer,
gru_classifier, cw, psk31, robust_rx) are **not stable between runs of the identical
tree**: two full runs of the same commit produced different subsets, and the ones that
fail inside the full suite pass when run in a smaller session. These examples host a
GRC server on a fixed port, so the signature is port contention / ordering, not a
datapath defect. Do not chase them as a regression without first re-running the subset
alone, and do not read the cascade count as a failure count.

**Gates:** 69 in `test_chacha20_qr.py`, all green — 2 RFC vectors on chip, 4 random
seeds, 7 wrapping corners (WRAPS, never saturates — a Q15 datapath would clamp and
fail), 8 anchors, 8 D4 orientations on the full burst, saturated == per-sample, 17 model-level
mutation gates each proven to fail (every rotate constant perturbed +-1 independently,
each of the four adds swapped for a XOR, the DROPPED CARRY, the rot16 hi/lo swap
reversed, frame word order reversed, hi/lo swapped, +1 frame shift, identity
passthrough, empty), 3 ON-CHIP mutants, and the structural gates above. Also
registered in the shared orientation (INV-23), saturation (INV-19), placement-legality
(INV-25) and GRC-binding (INV-22) suites.

## GardnerTimingRecovery SHIPS — the second quarantine's wall was a TOPOLOGY choice, not a substrate limit; and the DSP had a THIRD defect nobody had named 2026-08-27

Third attempt at the block quarantined 2026-08-06 and again 2026-08-27.
**Outcome: `done`.** GR `symbol_sync_cc(TED_GARDNER)` locks BER 0 across the full
fractional-offset sweep on the matched-filter Nyquist 2-sps channel, and so does the
block — reference (0/50 selection, 0/200 held out, lengths 150–2500) AND **on-chip**,
where the emitted stream is BIT-EXACT to `process_reference` (0 mismatches, identical
symbol counts) and saturated drive is bit-identical to per-sample drive. Two
independent defects were fixed, and it matters that they were independent: either one
alone leaves the block broken, and each masks the other.

### The DSP defect the previous two attempts BOTH missed: Gardner strobes ONCE per symbol

The 2026-08-27 entry recorded a Q15 recipe it had measured at BER 0 — modulo-1
counter + full-precision MULQ TED + GR-derived PI gains — and framed the remaining
work as purely build integration. **That recipe, re-implemented exactly as written,
does not reach BER 0 across the offset sweep at ANY gain — its best is 12 of 50
selection cases failing.** The missing piece is structural, and it
is the thing "Gardner is a 2-samples-per-symbol detector" quietly leads you into:

* the natural reading is a resampler that **strobes TWICE per symbol** and tags each
  strobe center/mid with a parity bit — which is exactly what the 2026-07 shipped
  block did, and what the 2026-08-27 retry kept.
* But that ties the mid sample's phase to a **separate strobe whose own timing the
  loop is still moving**. The TED's two operands then come from different loop
  states, the detector reads a geometry that never existed, and the S-curve is not
  well defined.
* The fix: **ONE strobe per SYMBOL**, and interpolate BOTH operands at that one
  strobe from one `mu` off one 3-tap window — the centre from `(x[n-1], x[n])` and the
  mid from `(x[n-2], x[n-1])`, one input sample earlier being exactly half a symbol at
  2 sps.

Measured, everything else held equal (same modulo-1 counter, same full-precision
MULQ TED, same GR gain derivation) and the two-strobe form given its correct strobe
rate and swept over loop_bw x ted_scale x max_deviation: **the two-strobe form's
BEST is 12 of 50 selection cases failing** — BER 0 on some offsets, 0.03-0.4 on
others, never 0 across the sweep, which is exactly the 0.04-0.12 band the original
2026-08-06 quarantine recorded. The one-strobe form is **0 of 50**, and 0 of 200
held out.

(A first pass at this comparison mis-set the two-strobe counter's nominal period,
which made it slip and report ~0.44 — chance. That number is wrong and is not the
claim; 12/50 is, and it is the more interesting result: the two-strobe structure is
not broken, it is merely incapable of closing the last few offsets, which is why it
survived two attempts looking almost right.)

The counter modulo, the full-precision TED and the GR gains are all necessary too,
but they are not sufficient, and the previous entry's confident "the signal
processing is done and re-derivable from this entry" was not true — the entry
omitted the one structural decision that carries the result.

Settled operating point (`loop_bw=0.02`, `damping=1.0`, TED-scale ×8 → K1=29162,
K2=1832 as Q15 MULQ multipliers, `max_deviation` 8192):

| grid | cases | failures |
|---|---|---|
| selection (5 seeds × 10 offsets) | 50 | 0 |
| held out (10 unseen seeds × 20 offsets) | 200 | 0 |
| held out #2 (20 fresh seeds × 6 offsets) | 120 | 0 |
| lengths 150/200/400/700/1200/2500 | 300 | 0 |
| ON-CHIP (3 seeds × 10 offsets, built + simulated) | 30 | 0 |

**Honest envelope, measured per cell of the sweep and recorded as a LIMIT:** peak
amplitude **0.5–0.75** (1/50 fails at 0.4, 7/50 at 0.2, 1/50 at 0.8, 25/50 at 0.9) and
RRC rolloff **β ≥ 0.35** (4/50 at 0.3, 8/50 at 0.25). The Gardner TED is
NON-decision-directed — it multiplies two SIGNAL samples — so its S-curve slope scales
with the SQUARE of the input level and the effective loop gain moves with drive
amplitude. That is also why the raw GR gain mapping needs a ×8 renormalisation here
where M&M needs none. Separately, the proportional gain is a Q15 MULQ multiplier and
**clamps above `loop_bw` ≈ 0.022**, so a wider requested loop is silently not
delivered; the default 0.02 sits just inside, and the gate asserts the ceiling rather
than hiding it.

### The on-chip wall: the previous entry's conclusion was RIGHT about the conflict and WRONG about it being a wall

2026-08-27 concluded: *"on this substrate a single cell cannot reliably be BOTH the
block's external-egress cell AND the source of an internal feedback trigger."* The
conflict is real and the four contending build passes it names are real. But it is a
consequence of **fusing** the two roles, not a property of the substrate —
`MMTimingRecoveryBlock` has had both an internal feedback loop and an external egress
since it shipped, because it **splits the roles across two cells**.

The fix is that split, and nothing else. `loop_filter` fans out: the recovered symbol
goes to a dedicated `qout` — one WRITE, one JUMP, one face, no state, no feedback —
and `v` goes to `period_relay` on a PERPENDICULAR face. `period_relay` is ordered LAST
in the program dict so its edge into `counter` is the block's ONLY backward
connection. With that, `_apply_internal_feedback` resolves the return on the first
try, patching both the `pout` data WRITE and the co-located lock-clear `WRITE.CFG`,
and the routed egress has nothing of the loop's left in it to clobber.

**NO ENGINE CHANGE WAS NEEDED.** `placekyt/engine/build.py` is untouched. The pass
ordering the previous attempt suspected of being defective is fine; what it could not
satisfy was a block asking one cell to hold two contracts.

**And the PARITY constraint dissolved.** The previous entry recorded, from an
exhaustive search, that the ring is a FIVE-cycle and a grid is bipartite, so "at least
one `transit_*` cell is mathematically REQUIRED, not stylistic." That is true of the
five-cell decomposition it searched — but splitting the delay line out of the NCO into
its own `dline` cell makes the ring
`counter → dline → interp → ted → loop_filter → period_relay → counter` a SIX-cycle,
which is EVEN, so it closes by abutment with NO transit at all. **Parity is a property
of the DECOMPOSITION, not of the block.** The shipped real-mode fold is 3x3, seven
cells, zero transits. (The
COMPLEX variant adds `qinterp` to the chain, making the ring a seven-cycle, which is
odd; that one genuinely does need a transit, and uses one.)

Getting there mattered for more than elegance, and the FOOTPRINT cost four
iterations — each invisible from inside the block, each found only by running the
designs it ships in.

1. **6x2 with a four-cell transit lane (11 cells).** Broke the shipped full-duplex
   BPSK modem: one of eleven nets stopped routing, taking 21 tests with it.
2. **7 cells / zero transits, folded 4 WIDE x 2 TALL.** Fixed the modem; broke the
   coherent-RX chain — a 4-wide block WALLS the matched-filter -> Costas bus
   channel (`no bus path from source to the broker tap`, 5/7 nets).
3. **2 WIDE x 4 TALL.** Fixed the coherent RX; broke the modem's auto-P&R instead
   (6 duplex-e2e tests). A TALL fold trips the packer's FIT-DRIVEN ROTATION of a
   feedback block whose authored height would overflow the current band
   (`autoplace._pack_compact`: `h > w and row_top + h > height` -> rotate `cw`).
   The flyline orienter deliberately leaves feedback blocks at identity, but this
   fit path overrides it, and once ONE block rotates the orienter re-orients
   everything downstream. Measured budget curve on the modem: 45 s -> 9/11 nets,
   90 s -> 10/11, 150 s -> 10/11, 300 s -> 11/11. Raising the budget would have
   turned an interactive operation into a four-minute one — not a fix.
4. **3x3, SQUARE.** Triggers neither behaviour: modem 11/11 in 2 s AND production
   coherent RX 7/7. Found by enumerating all 144 legal 3x3 zero-transit folds and
   scoring candidates against BOTH design families; the first one passed both.

**Two folds of the same area are NOT interchangeable, and the block's own suite cannot
see the difference** — both are BER 0, bit-exact and orientation-invariant. Search for
candidate folds by area and transit count (a ten-line exhaustive enumeration over chain
placements does it), then choose among the winners by RE-RUNNING THE DESIGNS, and
prefer wider-than-tall so the fit rotation never fires.

### Three ISA/control-flow traps, each of which produced a plausible wrong answer

The topology fix alone did not make it work. Three further bugs sat behind it, and all
three are worth generalising because each **looks like an arithmetic bug and is not**.

1. **A remote JUMP does not stop local execution — so a strobe-gated cell needs a
   `HALT`.** `interp` branched to `nostrobe` on the no-strobe sentinel and ended its
   strobe path with `{jump:trig}`. Without a `HALT` after that jump, the strobe path
   **fell through into `nostrobe:` and fired the no-strobe trigger as well**, so the
   loop_filter ran BOTH its entries every strobe and the second one zeroed the error
   it had just captured. Symptom: the integrator `vi` tracked the reference **bit for
   bit** while `v` came out exactly equal to `vi` on every single sample — i.e. the
   PROPORTIONAL term silently vanished and the 2nd-order loop degraded to a pure
   integrator. On-chip BER 0.22 against a reference BER of 0. (The FIR block's
   saturating-restore comment records the same hazard; it is not Gardner-specific.)
2. **`GOTO` compiles to a LOCAL JUMP, which has the same problem.** A `GOTO pi` used
   to skip a fall-through block is an opcode-0x7 word: it queues a re-entry and keeps
   running into the next word. Order the two entries so the path you want falls
   through naturally and the other branches FORWARD over it (`CMP Rz,Rz; BR.Z`) —
   MOVE does not touch the flags, so the compare must be explicit.
3. **An overflow-saturation's sign polarity is INVERTED from how it reads.** On an
   int16 `SUB` that overflows, the WRAPPED result's sign bit is the OPPOSITE of the
   true sign. So after `BR.NV`, clamp to `0x8000` when the wrapped value reads
   POSITIVE and to `0x7FFF` when it reads NEGATIVE. Getting it the obvious way round
   clamps to the wrong rail: measured, on a burst where `c - c_prev` overflowed
   negative, the chip produced `+32767` where the reference had `-32768`, the loop was
   kicked the wrong way, and it shed two strobes over the rest of the burst. It
   surfaced only on a stimulus that drives the TED difference out of range — the
   matched-filter channel at amp 0.7 binds it 4 times in 63,000 symbols, so a narrower
   test set would have shipped it.

### A structural fact worth keeping: a data WRITE is not routed, it is ABUTTED

The COMPLEX (I/Q) variant first placed its `qinterp` off to the side of the fold,
writing `yq` straight to `qout` four cells away. It places, routes, builds and runs —
and **the Q channel comes out all zeros** while the I rail stays bit-exact. An
internal data WRITE is delivered along the chain of abutting forward faces, and the
programmed cells in between are not transits: they do not relay it. A cell that sits
in the middle of a linear thread must forward EVERYTHING the thread carries, so
`qinterp` moved into the row-0 spine and relays the I rail's `(c, m)` as well as
producing `yq`, which then goes hand to hand `qinterp → ted → loop_filter → qout` —
the way MM walks its own recovered pair down to its egress. The I-rail-perfect /
Q-rail-zero signature is the tell.

### Also carried forward

* The **transit-face-overwrite** hazard from the previous entry held up, and the real
  fold now sidesteps it entirely by having no transits. The build test still asserts
  the feedback resolves in all 8 D4 orientations, because a rotation that puts a
  feedback cell under an external corridor kills the loop SILENTLY — the block still
  builds, still routes, and still emits at the correct rate while never adapting.
* **`_FACE_LOCK` is NOT `_FACE_FB`, and conflating them costs a day.** The counter's
  arbiter LOCK gates every face except the one the feedback ARRIVES on; `_FACE_FB` is
  the face it LEAVES the loop_filter on. In the 6x2 fold both happened to be SOUTH, so
  one constant served both and nothing complained. Re-folding moved the arrival
  face to EAST, the lock kept gating the wrong face, and the block emitted **exactly
  one symbol and went quiescent** — a signature INV-33 warns is indistinguishable from
  a state/instruction overlap. They are now separate constants with a test
  (`test_faces_match_the_layout`) that re-derives all three from `default_layout`.
* **The cold-acquisition transient is real and it is a behaviour change.** The loop
  starts at the nominal period with `v = 0` and needs **up to 6 symbols** to pull the
  offset in; from symbol 6 on, BER is 0 on every case. The block it replaces ran
  open-loop at the nominal period, so it had NO transient — and could not track a
  timing offset either, which is precisely why it was quarantined. Two downstream
  modem gates counted every symbol from zero and had to start skipping the transient.
  That is not a loosened tolerance: measuring a tracking loop's BER without excluding
  its acquisition is measuring the wrong thing, and the gates in question are about
  carried STATE, which the transient (identical on every repeat) says nothing about.
* **Re-seating a grown block: minimise ROUTE EXCESS, do not take the first fit.**
  Four saved designs hard-coded the old 4-cell placement and had to be re-placed.
  Taking the first anchor that merely ROUTED pushed `bpsk_modem`'s total route
  excess from its pinned 4 to 8 and `coherent_bpsk_rx`'s from 2 to 4, tripping the
  route-quality ratchet — which says, correctly, *"a route got longer; find out why
  before re-pinning."* Scoring every routable anchor by total excess instead put
  both BELOW their original pins (2 and 0), so the pins were tightened rather than
  loosened. The scoring loop is ~10 lines and it is the difference between
  degrading two shipped examples and improving them. Note also that the candidate
  scan must treat only other blocks' CELLS as occupied, not existing route cells:
  counting the routes the block's own nets currently use left the modem with ONE
  viable anchor (excess 8) instead of nine (best excess 2).
* **`CoherentRXBlock` was decoupled from this block.** It borrowed Gardner's
  `resampler` and `ted` cell programs, and it runs its timing stage OPEN-LOOP at the
  nominal period (its period feedback dead-ends by design — the 12-cell fold has no
  room for a second return corridor beside the Costas dphase corridor). It now owns
  local copies of the legacy two-strobe cells, so the fused receiver's silicon is
  **byte-for-byte identical** (verified: same 840-word bitstream, all 19 CoherentRX
  gates green) and the standalone block is free to be correct. A new gate pins its
  `process_reference` against its own chip output, because an over-modelled reference
  and the real chip both decode that burst at BER 0 — only a direct stream comparison
  separates them.

### GENERALIZES

**A quarantine entry's "the hard part is solved, only integration remains" is a
hypothesis, not a result — re-measure it before building on it.** This entry's
predecessor stated a Q15 recipe as settled and measured; re-implementing it exactly as
written does NOT reach BER 0, because the one structural decision that carried
the result was not in the write-up. The generalisable discipline is the one this
project already applies to code: **an entry that claims a measurement must contain
enough to REPRODUCE it**, and the reader's first act should be to reproduce it, not to
build on it. A three-line "what the loop does per sample" pseudocode block in the
previous entry would have saved the whole re-derivation.

Second: **when a control-flow bug hides inside a feedback loop, the loop's own state
tells you where.** `vi` matching the reference bit-for-bit while `v` never differed
from `vi` named the defect precisely — one term of a two-term sum was missing — and
pointed at the entry that zeroes the error rather than at any arithmetic. Dumping the
loop's intermediate state per sample and diffing it against the reference's, rather
than comparing only the output stream, is what turned a week-shaped problem into a
one-line fix.

## GardnerTimingRecovery retry — the quarantine's ROOT CAUSE WAS WRONG; the DSP is solved in Q15, the block stays quarantined on an ON-CHIP wall 2026-08-27

Second attempt at the block quarantined 2026-08-06. **Outcome: still `needs_human`, but
with a corrected and much sharper diagnosis.** The Q15 signal-processing question is
SOLVED and demonstrated (BER 0 across the full offset sweep in a bit-exact ISA model);
the block does not ship because the redesigned datapath does not close its feedback loop
on the built chip. Both halves are recorded below because both are durable.

### The 2026-08-06 root cause was WRONG (measured, not argued)

The quarantine record blamed TED PRECISION: *"the TED HALVES the BPSK sample difference
(>>1) to fit int16 and the resulting timing jitter closes the Nyquist eye"*, and
concluded the fix was *"a wider TED product without the >>1 truncation"*. That is not
what was limiting the block. An ablation on the exact quarantine channel/metric
(worst BER over 5 seeds x 10 fractional offsets, n=900):

| variant | worst BER | failing cases |
|---|---|---|
| V0 shipped (phase-accumulator NCO, halved MULHI TED, power-of-two gains) | 0.1220 | 40/50 |
| V1 + clamp the interpolation fraction | 0.0451 | 12/50 |
| V2 + full-precision MULQ TED (the documented "fix") | 0.0122 | 2/50 |
| V3 + GR-derived PI gains as well | 0.0415 | 14/50 |
| **V4 modulo-1 counter + MULQ TED + GR gains** | **0.0000** | **0/50** |

Removing the `>>1` (V2) helps but does NOT reach BER 0, and adding the correct loop
gains on top of the old NCO (V3) makes it *worse*. **Only replacing the NCO closes it.**
The real defect was an UNBOUNDED PHASE ACCUMULATOR:

* `phase` is a plain int16 accumulator with **no modulo**. Whenever the loop pulled the
  period below nominal, `phase` gained more per sample than each strobe shed, grew
  without bound, and WRAPPED int16 (measured `phase` reaching 32298 before wrap). The
  derived `frac = phase<<1` then read NEGATIVE and INVERTED the interpolation.
* So the loop was not jittering — it was **slipping**. That also explains the shape of
  the original evidence: a BER of 0.04–0.12 is far too good for a broken detector and far
  too bad for a working one; it is a loop that keeps re-acquiring and re-slipping.
* FIX: the MMTimingRecoveryBlock modulo-1 interpolator-control counter (Rice Ch.8) —
  `cnt = (cnt - W) & 0x7FFF` is bounded BY CONSTRUCTION, so no wrap is possible.

Two secondary defects were real and both are needed for cold ACQUISITION:

* **The loop gains were not GR's.** The `>>8` integral / `>>2` proportional shifts give
  effective gains 0.00195 / 0.125 against GR's derived 0.02492 / 0.19835 — the integrator
  was ~12.8x too weak to track the offset. Derive K1/K2 from `loop_bw`/`damping` with
  GR's own `control_loop` mapping (identical to `MMTimingRecoveryBlock._pi_gains`).
* **The TED threw away 2 bits for headroom it never needed.** A Q15 signal*signal MULQ
  product is ALREADY in [-1,1) (the MultiplyCC headroom finding), so pre-halving BOTH
  operands was paying 2 bits for nothing. Only the DIFFERENCE `c_k - c_{k-1}` can leave
  int16 — measured 6,063 of 44,387 strobes at amplitudes up to full scale — so saturate
  just that and use a plain `MULQ(mid, sat(c - cprev))`: **4.0x (exactly 2 bits)** more
  error amplitude. Those 2 bits are what let cold acquisition match GR's (GR is BER 0 by
  symbol 80 on every case; without them the loop needs ~300 symbols).

Measured, on the SAME channel and metric the quarantine used, in a bit-exact Q15/ISA
model (truncating MULQ, wrapping int16, immediate-count shifts): **BER 0 on all 50
selection cases AND on 200 HELD-OUT cases** (10 unseen seeds x a 20-point offset grid),
plus lengths 200–2500 — at `loop_bw=0.02, damping=1.0` with a x4 TED-scale normaliser
(K1=14581, K2=916 as Q15 MULQ multipliers).

**Operating envelope (honest limit, not hidden):** the loop is amplitude-sensitive
because the TED error shrinks with signal level — clean at amp 0.4–0.95 and rolloff
beta >= 0.25, degrading below that. The verification channel (amp 0.7, beta 0.35) sits
well inside.

### Why it STILL does not ship: the redesign does not close its loop on-chip

The redesigned datapath needs 5 cells (the NCO cell cannot also hold the delay line +
two interpolations + a saturating TED — fused it needs 36 words in a 32-word cell). Every
cell fits, the block places, routes and builds clean, the recovered symbol egresses at the
CORRECT RATE (424 outputs for 424 symbols), and `ted.cprev` tracks the reference exactly —
but **`period_relay` never executes**, so the PI never runs, `counter.v` stays 0 forever,
and the timing loop never adapts (on-chip BER ~0.03–0.15 against a reference BER of 0).
Everything static checks out: entry address 20, a 1-hop WEST abutment, the correct
`face_fb`, and the build's own feedback pass reports BOTH the data WRITE and the
co-located lock-clear `WRITE.CFG` patched to `@2`/dest 6 (`matched=True`). The trigger
still does not fire the cell.

**THE EXACT WALL: on this substrate a single cell cannot reliably be BOTH the block's
external-egress cell AND the source of an internal feedback trigger, once the block has
more than one cell between the landing cell and that egress.** Four distinct build passes
each claim the exit cell's WRITE/JUMP words, and they conflict:

* `output_at_last_write` patches the cell's HIGHEST-ADDRESS WRITE with the output hop —
  a SINGLE-WRITE contract. It cannot express "the last write of the strobe path but not
  of the no-strobe path", so a two-entry egress cell always mis-patches one path.
* `_apply_routes` rewrites EVERY WRITE in a ROUTED exit cell to the output corridor,
  and the `feedback_blocks` preserve-set only protects cells that are themselves
  feedback SOURCES in chain order.
* `_apply_internal_feedback` re-patches the cell's highest-address JUMP when the
  feedback edge is declared as a connection — which is the EXTERNAL EGRESS trigger.
* Reordering the cells to make the feedback edge backward fixes the WRITE and breaks
  the JUMP, and vice versa. Both orderings were built and measured.

Observable signatures, all of which look like unrelated bugs:
* egress WRITE left at its authored `@1` -> builds and routes clean, emits **NOTHING**;
* feedback WRITE repointed at the output corridor -> `period_relay` never runs, the lock
  never clears, block emits **exactly ONE** symbol then goes quiescent (INV-33 warns this
  is indistinguishable from a state/instruction overlap);
* strobe gating lost -> **exactly 2x** the expected output count (one per input sample
  rather than per strobe).

Two further durable facts found on the way:

* **A block's internal ring has a PARITY constraint.** The loop
  counter -> interp -> ted -> loop_filter -> period_relay -> counter is a FIVE-cycle, and
  a grid graph is bipartite, so it admits only EVEN cycles: no placement of those five
  cells closes the ring by abutment alone (verified by exhaustive search over 3x3, 4x3,
  3x4 and 4x4). At least one `transit_*` cell is mathematically REQUIRED, not stylistic.
* **A `transit_*` cell's authored face is NOT safe on the block perimeter.** The route
  pass runs BEFORE the feedback pass and overwrites `fwd_face` on every cell a corridor
  crosses; the materialisation only applies the authored face when the cell is absent
  from the cell_map. A perimeter transit crossed by the input corridor had its NORTH
  face rewritten SOUTH, the feedback trace dead-ended, and the loop silently never closed.
  MMTimingRecoveryBlock survives this only because its transit LANE runs along a row no
  corridor uses.

### For whoever picks this up

The signal processing is done and re-derivable from this entry; the remaining work is
purely build/router integration. The most promising route is a DEDICATED single-face
egress cell (the MM `qout` idiom: exactly one WRITE, one JUMP, one face) that is BOTH the
last program cell AND the router's block exit, with the feedback source kept strictly
upstream of it — i.e. make the egress contract single-valued by construction rather than
trying to satisfy four patch passes at once. That shape was attempted here and hit the
`_set_cell_hop1` zeroing of `pout`'s destination when `period_relay` became the last
cell; resolving THAT ordering conflict is the next concrete step.

Artifacts: the full redesign (reference + 5-cell programs + layout) and the ablation
harness were kept out of the commit deliberately — the repo must not carry a block whose
reference disagrees with its own silicon.

**GENERALIZES:** when a quarantine names a PRECISION root cause, ablate it before
believing it. Precision defects degrade gracefully and roughly uniformly; this one
failed 40/50 cases at 0.04–0.12 BER, which is the signature of a loop that LOSES LOCK,
not one that jitters. An unbounded accumulator in a feedback loop is the thing to look
for first, and the cheapest possible check — print the accumulator's max magnitude
against its int16 range — would have found it in minutes.

## FFT128 DISPLAY — a bit-exact chain is not a correct PLOT, and the drawn-trace tap is what proves it 2026-08-25

`examples/fft128_2p2s` computed correctly and its plot was still wrong. Reported
watching the GUI: *"it doesn't show the actual frequency where the spikes are ...
that still has time as the x axis and those spikes just flow across the screen."*

**The `.grc` plotted the kyttar sink's raw recovered stream on a `qtgui_time_sink_x`.**
A time sink's x axis IS time — no relabel makes it read in Hz. And the stream is not a
spectrum: **four** transformations separate them, and skipping any one leaves a plot
that still looks plausible.

| # | Transformation | What its absence paints |
|---|---|---|
| 1 | **de-interleave** the complex pair | the tail is a COMPLEX exit cell (`out_i`, `out_q` from one cell) — **two** float words per bin, drawn as two adjacent time samples |
| 2 | strip the **N−1 latency** | the zero-fill startup transient read as a frame |
| 3 | **un-reverse** the DIF slots | a SCRAMBLED spectrum: clean lines, wrong frequencies |
| 4 | **fftshift** | natural order runs 0 → +fs/2 then *jumps* to −fs/2; no linear axis can label it |

The fix is the one `examples/fft_spectrum` already shipped, so the transferable rule is
**a placed FFT's example needs a display chain, not a scope** — and if a sibling
example already has one, copy its shape rather than re-deriving it. What differs per
example is only *N* and whether the chip already reduced I/Q to power on-chip
(fft_spectrum has a `ComplexToMagSquared` cell; FFT128 does not, so the de-interleave
moves into the display block).

**THE PART WORTH GENERALISING: tap the DISPLAY, not the sink.** With the chain
bit-exact and the display chain in place, the plot still **blanked on every third
frame**. `burst_len` is 384 while `latency + 2*n_fft` is **383**, so every burst ends
with ONE sample left over; a frame reader that consumes across the boundary builds its
next "frame" from that 1 real sample plus 127 of the NEXT burst's zero-fill — an
all-zero spectrum, repeating forever under `server_repeat`. Measured: **4728 frames in
a perfectly regular good/good/blank cycle.**

**No bit-exactness assertion can see this.** The sink stream was byte-perfect; the
fault is purely in the display glue's framing. It was caught only because the gate taps
`spectrum.0` / `to_db.0` — the display blocks' own output, what the vector sink is
actually painted with. This is the same lesson the CSS transceiver's display gate
records (a free-running reference sliding against the decode), now confirmed on a
second, structurally different example: **a user-path gate that stops at the kyttar
sink is testing the wrong thing.**

`verification/grc_userpath_run.py`'s optional third argument (extra `block.port` taps)
now sizes the tap's vlen from `output_signature().sizeof_stream_item(port)`, so a
**vector** display port can be tapped at all — a vlen-1 `vector_sink_f` on a 128-float
port is an itemsize mismatch and the flowgraph refuses to connect.

**Measured, on the drawn trace through the hosted server:** two ON-BIN tones at natural
bins 9 and 37 of 128 land at **+2250 Hz** (power 0.2025, −6.93 dBFS) and **+9250 Hz**
(0.1225, −9.12 dBFS) at `samp_rate = 32000` (250 Hz/bin), with every other one of the
128 points at exactly 0.0 — an on-bin tone leaks nowhere. Slots 72 and 82 off the chip;
`bit_reverse_7` puts them back; the fftshift moves them to centred indices 73 and 101.

Mutations gated to FAIL: no un-reversal, a wrong-*N* (6-bit) un-reverse map, no
fftshift, raw words as a time series, no de-interleave, an unstripped latency, a halved
`samp_rate`, and degenerate flat/zero/short vectors. Plus the `.grc`'s own inline
stimulus expression is **evaluated in its own variable scope** and required to equal the
stimulus the gates drive — so "the tones are at 2250/9250 Hz" cannot quietly become a
claim about a different stimulus than the user runs.
## A qtgui time_sink does NOT paint a NoPen channel above channel 0 — and a correct demo that LOOKS wrong is a broken demo 2026-08-25

Two lessons out of the `examples/css_transceiver` display rework. The chip was never
at fault: SER 0 on segment A, `KYTTAR CSS` recovered, bit-exact against the composed
golden, throughout. Everything below is display.

**1. THE RENDERING DEFECT — line style 0 (NoPen) silently drops every channel above
channel 0.** A `qtgui.time_sink_f` with `nconnections=2`, both channels set to
`set_line_style(i, 0)` (NoPen — "markers only", which is what GRC's style option `0`
means), draws **only channel 0**. Channel 1 is never painted, at any marker id and any
line width.

Reproduced standalone, outside the example, with two `vector_source_f`s of
**different amplitude** — so it is not occlusion, the second trace genuinely is not
drawn — and confirmed on a real X display as well as under `QT_QPA_PLATFORM=offscreen`,
so it is not a headless artifact either. Give the same channel any real pen
(`set_line_style(i, 1)`, Solid) and its markers appear immediately. Channel 0 with
NoPen paints its markers fine; only the higher channels vanish.

This had been shipping. The example's scope used `style: '0'` on all four traces, so
**the decoded traces the demo exists to show were never actually rendered** — the
window displayed the reference traces alone and looked, reasonably, like a chain
producing nothing. Rule: **never leave a multi-channel time_sink trace on style 0.**
The example's gate now rejects it structurally.

Corollary once both traces DO paint: the highest-numbered channel is painted LAST, so
put the trace that must survive an exact overlay there. Reference on input 0 as a wide
circle, decoded on input 1 as a narrower X — an X inside a ring reads as a lock, a
bare ring or a bare X reads as a miss. Wired the other way round a perfect decode is
invisible under its own reference, which is a *different* way to make SER 0 look like
a dead chain.

**2. A DEMO WHOSE CORRECT OUTPUT READS AS A FAILURE IS A DEFECT, even with every
number right.** This example ships a deliberate on-chip negative control: segment B at
−10 dB, whose decode must collapse, because that is what makes segment A's SER 0 a
measurement rather than a chain that cannot fail. With A and B drawn as four traces on
ONE axis it was reported as "the +10 dB works flawlessly but the −10 dB doesn't work
at all" — a precise description of the demo behaving exactly as designed. One axis
carrying both a lock and an intended collapse cannot say which is which; a viewer sees
half the points miss and concludes half of it is broken.

The fix is layout, not DSP: **one panel per segment, each title carrying its own
verdict** ("SEGMENT B · −10 dB · NEGATIVE CONTROL — the X marks MISS their circles ✓
EXPECTED, THIS IS THE POINT"), plus the **measured SER of each segment published as a
live number** beside the panels. Nobody should need the README open to interpret a
plot. Gate the structure, not just the data: the example's gate now reads the shipped
`.grc` and asserts the panel count, the per-panel channel split, the wiring
orientation, the line styles, the marker distinctness, and the words `CONTROL` /
`EXPECTED` in B's title.

**3. A NOISE-DRIVEN CONTROL WANTS A BAND WITH TWO MEANINGFUL ENDS.** The old assertion
was `ser_b > 0.2`. It catches a control that quietly starts decoding and nothing else —
in particular it happily accepts a **dead chain**, because a chain that has stopped
computing and emits a constant scores a very HIGH SER. Scored against this 24-symbol
frame, the 16 stuck-at-k streams score 0.7500 (k=5), 0.7917 (k=0, k=4) and up to
1.0000; uniform-random guessing over the 16-ary alphabet averages 0.9375 and
effectively never drops below 0.667. The band is therefore **[0.40, 0.75)**: the floor
rejects a decoding control, and the ceiling — set at the cheapest stuck-at score —
rejects every constant-output chain. Measured on the shipped burst: **0.6250** (15 of
24), comfortably inside, and *below* the random floor, which says something real about
segment B: at −10 dB the decode still retains partial signal and beats chance.

**4. A HEADLESS TRACE GATE CANNOT SEE ANY OF THIS.** The four-channel tap gate was
green while defect 1 was live — it asserts the DATA fed to the scope, and that data was
always correct. What found it was rendering the flowgraph's Qt window offscreen
(`tb.grab().save(png)`) and *looking at the picture*. When a display is the deliverable,
render it and look; the data gate is necessary and is not sufficient.

## FFT128 user path CLOSED — a RAW-vs-Q15 encoding mismatch that ALIASES, and why it read as a re-framing 2026-08-25

The last open gap in `examples/fft128_2p2s` — `test_fft128_2p2s_shipped_grc_user_path`
was `xfail`, "the hosted repeat-loop burst is not this stimulus's transform" — is
closed. **It was never a sink or batch-session fault.** It was the `.grc`.

**ROOT CAUSE.** `kyttar_source`'s `output_words="auto"` ties **raw int16** output to
`complex_in`. That is the **bit-packing receiver** convention (a slicer's decoded bit
lives in the word's LSB, which Q15 scaling would crush). The FFT128 chain is the
opposite case: its output is a **Q15 VALUE** — the transform's bins. Left on `auto`
the sink emitted raw ±30000 word floats while every consumer applied the documented
q15/32768 convention, `round(w × 32768) & 0xFFFF`, under which raw words **alias**:

| chip word | sink emitted | decoded | should be |
|---|---|---|---|
| `0x399a` (14746) | `14746.0` | `0x0000` | `0x399a` |
| `0x2ccd` (11469) | `11469.0` | `0x8000` | `0x2ccd` |
| `0x0000` | `0.0` | `0x0000` ✓ | `0x0000` |

**WHY IT HID — and this is the transferable part. Zero is a FIXED POINT of the
aliasing.** `0.0` decodes to `0x0000` either way. This `.grc` drives two pure tones,
so a correct 384-sample transform has exactly **4** non-zero samples; only those
aliased. The burst came back **4/384 wrong** — looking almost right rather than
obviously broken. A scaling bug on a sparse signal corrupts only the signal, which is
precisely the part a "does it look busy" glance skips.

**THE MEASUREMENT LESSON, which cost a whole debugging cycle.** The earlier recorded
signature — "non-zero indices pairs 64 apart with period 192, versus the reference's
10 apart with period 128" — pointed at a batch/frame-boundary fault in the sink, and
that hypothesis was written into the gate, the README and this log. It was wrong. A
derived **index pattern is not a root cause**; it is a shadow of one, and it can point
confidently at the wrong layer. What actually closed this was instrumenting the
**boundary**: monkey-patch `MultiChipSimServer._process_batch_multichip` and print
what the server RECEIVED against what it RETURNED. That took one run and showed the
server returning `14746.0` — correct data, wrong encoding — immediately. *When a
value is wrong, compare the two sides of each interface it crosses before theorising
about which layer is at fault.*

**A THREE-WAY BIT-EXACT RESULT DID NOT NARROW IT.** Headless was bit-exact, and the
server's own drive+demux shape offline was bit-exact at every event budget (4000 /
60000 / 200000). Both are true and both are **irrelevant**, because neither carries
the header's `raw` flag through the encode/decode round trip. *An offline
reproduction that skips the field you have not suspected yet proves less than it
appears to.*

**THE `.grc` PARAM WAS ALSO SILENTLY REWRITTEN.** Both FFT `.grc`s carried
`output_words: 'False'` — a stale boolean from before the enum existed. It matches no
option, and GRC **silently resolves an unrecognised enum value back to the default**
(`"auto"`) rather than erroring. Same for `repeat: '''yes'''` (double-quoted, matching
neither `yes` nor `no`), which fell back to `no` — the correct value, by accident.
*A `.grc` enum that does not match an option is not a build error; check the
GENERATED Python (`kyttar.source(..., output_words="auto")`), never the `.grc` text.*

**BLAST RADIUS.** The sibling two-die example had the **identical** defect — the
two `gen_grc.py` files are the same file modulo comments, so the fault was cloned
— and had **no user-path gate at all**, so nothing would have caught it. Both
were fixed (`output_words='"q15"'`, `repeat='no'`) and each gained a user-path
gate. (That sibling has since been retargeted onto the real 2P2S board as
`examples/fft128_2p2s/`, whose gate is
`test_fft128_2p2s_shipped_grc_user_path`.) Every other value-output complex example already set `"q15"`
(`fft_spectrum`, `cordic_polar`, `complex_math`, `lms_equalizer`, `fm_transceiver`);
the two FFT128s were the outliers. *When you fix an example generated from a
copied script, grep for the sibling — and if the sibling has no gate, that is the
finding, not a footnote.*

**THE DISPLAY SAID IT TOO.** Raw ±30000 against the scope's `ymin/ymax` of −1/1 is a
flat off-scale line, and the `.grc`'s own comment already claimed the plotted stream
was "at the q15/32768 scale". The generator's comment and its param disagreed, and
the comment was right. Same failure class as the LMS equalizer's
missing-constellation report.

**GATE.** The `xfail` is gone, replaced by a bit-exact assertion plus a
non-vacuity check (the 4 energy-bearing samples must be non-zero, so a dead chain
cannot pass on the zeros alone) and `server_repeat` repetition integrity. Teeth
proven by reverting the `.grc` to `"auto"`: the gate FAILS with exactly the 4/384
signature. 8/8 user-path gates green (serial — port 58950 contends), 87 in the FFT
headless suites, 33 multichip/port_config, 90 grc valid+instantiate.

## css_transceiver — a BIT-EXACT chip that LOOKED broken: two scope-display defects a green gate could not see 2026-08-25

The owner ran `examples/css_transceiver` through the real workflow (open the
`.kyt`, *Run as GNURadio Server*, open the `.grc`, Run) and reported that the
"DECODED vs TRANSMITTED" scatter showed decoded symbols that **did not match**
the transmitted ones — a smear. The shipped gate said SER 0.0000 with
`KYTTAR CSS` recovered, 12/12 green. The gate and the plot disagreed.

**The chip was right and the gate was right; both were answering a question
nobody was asking.** Tapping the actual flowgraph blocks that FEED the scope
(not the kyttar sink) showed segment A decoding perfectly — 24/24 symbols, SER
0 — through the real client stack. The display was the defect, twice over:

- **1. PHASE DRIFT between two independently-rated producers.** The transmitted
  reference was a separate `blocks_vector_source_x` wired to channel 1 of the
  time sink. It free-runs; the kyttar sink's stream is gated by the simulator's
  batch turnaround. **Measured over one 60 s run: 75130 reference items vs
  58750 decoded items — the reference produced 27.9 % more.** A QT `time_sink`
  pulls the SAME count from every channel, so the reference slides against the
  decode. Quantified: **an offset of just 3 items makes 22 of segment A's 24
  correct symbols render as mismatches.** This is the general trap — *never
  feed one scope from two producers whose rates are set by different clocks;
  a chip-gated stream and a free-running vector source are never in phase.*
  The cure is structural, not a tweak: **derive the reference from the item
  index of the same stream you are decoding**, inside one block, so the two
  cannot drift by construction.
- **2. NEGATIVE-CONTROL CONFLATION.** The burst deliberately carries segment A
  (+10 dB, must decode) and segment B (−10 dB, must collapse). Both were drawn
  on one axis with nothing marking which was which, so **17 of 50 plotted
  points disagreed BY DESIGN** and the plot could not say so. An on-chip
  negative control is only worth shipping if the display makes it legible as a
  control — otherwise it just looks like the demo failing.

The fix: `css_decode_map.py` became a 1-in/**4-out** block emitting
`A decoded / A reference / B decoded / B reference`, all phase-locked to one
stream, each segment blanked (NaN) outside its own half so neither overplots
the other. The framing-latency word (word 0 of each segment, which carries no
data symbol) is blanked on BOTH channels — plotted, it is a lone unmatched
point on an otherwise exact overlay and invites exactly the wrong question.

**The gate lesson, which generalizes: a user-path gate that asserts only the
recovered SINK stream is testing the wrong thing.** The sink is not what the
user looks at; the display glue downstream of it is. `grc_userpath_run.py`
gained an optional third argument naming extra `block.port` taps, and the CSS
gate now asserts the DRAWN traces — A matching at every plotted point, B
visibly missing, the two disjoint, every frame identical across the run — with
four mutation tests that replay both original defects and prove the gate fails
on them. 13/13 green. The `.kyt` regenerates byte-identical: the chip never
changed, only what was drawn about it.
## fft_spectrum LEGIBILITY pass — a synthesised I/Q sibling has no NET to name a trace after 2026-08-25

Three user reports on the shipped `examples/fft_spectrum` (both sizes), opened
cold in the GUI. One was a real defect, two were missing explanation. The
general lesson is the first one, and it applies to **every complex example**.

- **1. LABEL BUG (real, fixed).** "Why do both inputs say `xi`? Is this complex
  or not?" — the waveform pane labelled BOTH `x16_in` rails `fft64.xi`.
  Root cause: GNU Radio collapses an I/Q pair into ONE complex port, so the
  importer (and `add_logical_connection`) wires only the I-half and
  **SYNTHESISES** the Q-half. The project therefore stores **one** connection
  (`x16_in -> fft64.xi`) for a port that physically carries **two** tagged
  rails. `MainWindow._port_tag_name` named a tag by looking up the nets touching
  the port and, with exactly one net, returned that name **for every tag** — so
  both rails got the same string. Fix: `_iq_rail_name` resolves the Q-half port
  via `grc_import._iq_sibling` and picks the rail from the tag's `dest` register
  against `catalog.resolved_io`'s two input registers. **Generalizes:** any
  single-net complex block input had this collision — the sweep found it also on
  `channel_selector` (`floattocomplex.re/.im`), `css_transceiver`
  (`conjchirpmixer.xi/.xq`) and `lms_equalizer`, all now split correctly. Real
  scalar ports (`gain.sample`) are untouched, because `_iq_sibling` returns None.

  **The DATA was never wrong** — and that mattered, because the same symptom
  ("both rails look like one") is also what the un-named-stream landing bug
  produces, and that one IS a data defect. Distinguish them by READING the rails:
  drive the built chip with `enable_trace()` and pull
  `TraceModel.port_streams_by_tag()`. Measured here, N=64:
  `xi = 29491, 13902, -16384, -29349, …` and `xq = 0, 26009, 24521, -2891, …` —
  the tone's cosine and sine, bit-exact vs the reference, `xq[0] = sin(0) = 0`.
  That read is now a gate at both sizes.

- **2. A BIN INDEX IS NOT A FREQUENCY.** The x axis read "FFT bin (natural
  order)", which is honest but useless — and it was the only honest label
  available, because natural order runs `0 -> +fs/2` then JUMPS to `-fs/2`, and
  no linear axis can label a discontinuity. The fix is two halves: publish the
  map (`f(k) = k*fs/N`, `(k-N)*fs/N` for `k >= N/2`) at a DECLARED sample rate —
  the array is asynchronous, it has no clock, so `samp_rate` is a property of the
  stimulus, not the chip — and **fftshift the display vector** so the axis is
  monotonic and `set_x_axis(-samp_rate/2, bin_hz)` labels it. Measured on the
  real chip at `samp_rate = 32000`: N=64 peaks at point 43 = **+5500.0 Hz**
  (500 Hz/bin), N=32 at point 27 = **+11000.0 Hz** (1000 Hz/bin) — same bin
  index, twice the frequency, because a half-length transform has twice the bin
  width. Keep ONE copy of the arithmetic (here `bin_to_hz`/`axis_hz` in the demo
  module) and have the README, the `.grc` axis config and the gates all cite it.

- **3. A PORT TRACE IS A WORD STREAM, SO A STAIRCASE IS CORRECT.** "The waveform
  isn't a sinusoid." It is — the pane draws the Q15 WORD stream, one step per
  sample, and the shipped stimulus has `N/tone_bin` samples per cycle: **5.8** at
  N=64 bin 11, **2.9** at N=32. Under ~10 samples/cycle nothing looks smooth.
  Don't "fix" the trace; make the example legible instead — ship a `time_sink`
  on the stimulus itself and document the escape hatch (`tone_bin = 1` gives one
  whole cycle per frame). **Gotcha:** the ≤ burst−16 scope-sizing rule (a QT
  `time_sink` paints nothing until a FULL buffer arrives, and the scheduler
  strands a finite stream's tail) applies only to scopes on the FINITE chip
  stream. This scope taps the `repeat = True` vector source, which streams
  forever — measured, a 256-sample scope on the N=64 source receives 768+
  samples. The first draft of the gate asserted the burst rule here and failed
  correctly; the real invariant to assert is that the source still repeats.

Gates: `verification/tests/test_fft_spectrum_example.py` 26 -> **51**, all green
standalone on port 58950 (both live user-path gates included). New mutations
proven to FAIL: duplicated / swapped / empty / delayed input rails, the old
single-net namer rule (reverting the fix fails 3 gates with
`assert 'fft64.xi' != 'fft64.xi'`), a bin->Hz map that ignores the sample rate,
and an axis read without the fftshift (natural bin 11 on the centred axis reads
-10500 Hz, not +5500).
## FFT128 retargeted to the 2P2S board + the "dies don't run in parallel" report 2026-08-25

`examples/fft128_2p2s` replaces the ad-hoc two-chip `fft128_2die` project with the
real **2P2S dev board** (`resources/boards/dev2p2s.kdb` — four dies, two parallel
daisy-chains). Same verified split (die 0 = stage 0, die 1 = stages 1..6), now on
chain A's head and tail, joined by the board's own **on-carrier series link**.
200/200 samples bit-exact, 400 words, DRC-clean against the board file.

**Retarget lessons, all cheap once known:**

- **Instantiate ALL the board's dies, not just the ones you use.** A 2-die design
  on a 4-die board is still a 4-chip project: `add_chip()` x3, label them the way
  the board does, and wire BOTH chains' carrier links. This is what lets
  `engine.drc.check_project(..., board=...)` run at all — `_check_inter_chip`
  verifies every declared link is a wire the carrier physically provides, and a
  two-chip project simply has no board to be checked against. The idle chain's
  dies still build (~34 cells of port infrastructure each) and must stay empty.
- **`.kyt` carries a `board:` ref** (`project.board = BoardRef(name, config)`) and
  it round-trips. Neither shipped multi-chip example set it before; without it an
  opened design does not know which board it targets.
- **Give the board DRC teeth.** A cross-chain link (chip0 -> chip3) must be
  REJECTED, or "DRC clean against the board" may just mean the check is inert.
- **Assert the unused chain is SILENT** — no egress words and no trace events —
  or the "two independent chains" property the board rests on is unproven.

**THE CONCURRENCY REPORT — the answer has two halves, and they differ.** The cell
animation looked like chip 0 ran to completion and only then chip 1 started.

1. **The engine does NOT batch.** Measured per-die trace events per trigger over
   200 samples: *every* trigger has die 0 doing ~1107 events and die 1 ~2900.
   Neither die is ever idle while the other works. There is no "run die 0 over the
   whole stimulus, then hand die 1 a block".
2. **Within ONE trigger the dies ARE sequential — and that is CAUSAL, not a
   scheduler artifact.** Die 0's crossing word for a sample is the LAST thing it
   produces: its egress reaches the port cell (9,0) at event **1208 of a
   1209-event burst (99.9%)**. Die 1 cannot start earlier because there is nothing
   to start on. At single-round granularity: `JUMP round 0: c0=1209 c1=0`,
   `JUMP round 1: c0=0 c1=2877`, `round 2: QUIESCENT`.
3. **Do NOT try to fix this by shrinking the round budget.** `run(events, rounds)`
   is events-per-chip-PER-ROUND. Measured at budgets of 400/200/60, the number of
   rounds where both dies advanced was **10 in every case** — one handoff round
   per sample, never more. The budget is not the lever; the causal dependency is.
   (What genuinely overlaps on hardware is sample k+1 in die 0 against sample k in
   die 1 — pipelining ACROSS samples, which the per-sample drive deliberately does
   not do because the three-part complex transaction must be pumped to quiescence.)

**The ANIMATION half WAS a real bug.** `sim_controller`'s multi-chip refresh built
its flash-step list by **concatenating each chip's steps in chip order**
(`for cid in sorted(by_chip): m_steps += ...`), and the canvas replays that list in
order — so chip 0's whole burst played before chip 1's regardless of what the
engine did.

> **The trap worth remembering: sorting the merged steps by `time_ns` does NOT fix
> it.** Each chip keeps its OWN sim clock and the clocks DIVERGE — measured, die 1's
> clock runs **2.27x** die 0's after 200 samples, and the gap grows every sample
> because die 1 does more work per sample. A strictly-later clock never interleaves
> with a strictly-earlier one, so a global time sort reproduces the identical
> batched playback. **Per-chip sim clocks are not a shared time base.**

Fix: `SimController._interleave_chip_steps` merges round-robin on each chip's
progress through its OWN burst. Rendering order only — moves no data, changes no
arithmetic. Gated (with teeth against the old concatenated order) by
`test_the_animation_interleaves_the_dies_rather_than_batching_them`, which needs
no Qt and no simulator.

Measured on a real 6-sample run (24,489 chip-tagged events → 15,802 flash steps),
which chip lights on each of the first 24 steps:

    OLD: 0 . 0 0 0 0 0 . 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0
    NEW: 0 1 . 1 0 1 0 1 0 1 0 1 0 1 . 1 0 1 0 1 0 1 0 1

    OLD: chip 1's FIRST flash is step 4687/15802 — 29.7% into the animation
    NEW: chip 1's FIRST flash is step 1/15802    —  0.0%

So under the old order chip 0 animated ALONE for the first 29.7% of the run — an
exact match for the "chip 0 works for a long time with chip 1 idle" report. This
is a good template for the general problem: when a visual claim is disputed,
derive the same structure the renderer consumes from REAL trace data and diff the
old vs new ordering, rather than arguing from the code.

**TWO LIVE-PATH DEFECTS the headless gates could not see** — found only because the
user-path gate was written. Headless was green at 200/200 bit-exact while the hosted
`.grc` returned a stream of two distinct values. Both are in the MULTI-CHIP bridge:

- **A chain continuing across the CARRIER WIRE resolves `out_tag=None`.**
  `port_config.stream_targets` finds a chain's egress tag by walking block -> block
  WITHIN ONE CHIP. When the stream's input net is on the head chip and the tagged
  egress net belongs to the tail chip — joined by an INTER-CHIP WIRE, not a
  block-to-block net — the walk never reaches it. The tail's words ARE tagged on the
  fabric, so `None` makes the host demux drop every one: **data flows on chip and the
  flowgraph shows nothing.** Fixed with `_tail_egress_tag`, applied only when the
  chain genuinely spans chips (single-chip behaviour untouched).
- **The multi-chip demux kept only ONE tag of a COMPLEX PAIR.** A complex exit cell
  emits I then Q from ONE cell on tags `(out_tag, out_tag+1)` — measured at the tail:
  `{7: 140, 8: 140}`. The single-chip path already owns both (`_tag_owner`); the
  multi-chip drain matched `d == out_tag` only and discarded every Q word. More
  dangerous than the first: the stream arrives at HALF LENGTH with the imaginary part
  gone, which reads as a plausible wrong answer rather than an obvious failure.

> **Generalizable:** only the I rail is wired to a net (wiring a second net to the same
> port kills egress), so the fabric emits a tag **the project graph never mentions**.
> Deciding "is this egress complex?" from the NETS alone cannot work for a chip-scale
> complex block — it must come from the terminal block's DECLARED output registers.
> The single-chip resolver already OR-ed both sources; the multi-chip one had neither.

**STILL OPEN (recorded, not hidden): the repeat-looping SINK republishes a burst that
is not the stimulus's transform.** After the two resolution fixes the hosted path
resolves its stream, returns words, and delivers BOTH rails — but the emitted burst is
wrong. Measured three ways on the identical stimulus:

| driven through | result |
|---|---|
| headless (`MultiChipSimEngine`) | BIT-EXACT 768/768, zero saturated words |
| the server's OWN drive + demux, offline | BIT-EXACT 768/768 |
| the HOSTED sink (`server_repeat=True`) | 1,359,360 words; non-zero SAMPLE indices 104, 168, 296, 360, 488, … — pairs **64** apart repeating every **192**, where the reference has pairs **10** apart repeating every **128** |

Amplitude/saturation was RULED OUT: bit-exact at 0.45/0.35 and at 0.25/0.20 and
0.15/0.12, zero saturated words in every case. So chip + crossing + target resolution
+ tag demux were all correct.

> **RESOLVED 2026-08-25 — and the sink/batch-session hypothesis above was WRONG.**
> The third fault was the `.grc`'s `output_words="auto"` on a VALUE-output chain:
> the sink emitted RAW word floats that ALIAS under the q15/32768 convention every
> consumer applies. See the top-of-log entry. The "period 192 / pairs 64 apart"
> reading in the table above was an artifact of measuring the aliased stream; on a
> clean re-measurement the corruption is 4 samples of 384, at the reference's own
> non-zero indices. **Lesson about the measurement itself: a derived index pattern
> is not a root cause.** Re-measure before theorising about a layer, and prefer
> instrumenting the actual boundary (what the server RETURNED vs what the client
> DECODED) over inferring structure from indices.

**A LIVELINESS HEURISTIC IS THE WRONG GATE — bit-exactness is the right one.** The
first version of that user-path gate asserted the recovered stream "looks busy"
(enough distinct values, enough non-zero words). That is WRONG for this flowgraph:
the `.grc` drives two pure tones at exactly bins 9 and 37 of 128, so a CORRECT
transform is nearly all zeros — measured, **3 distinct values over 768 words**. The
heuristic fails on a CORRECT chain, and the natural next move is to weaken it, which
would have left a gate that proves nothing. Assert equality with the reference for the
`.grc`'s OWN embedded stimulus instead: it cannot pass on a broken chain and cannot
fail on a working one. (This also catches the half-length complex bug for free — a
one-tag demux returns exactly half the words.)

**Gate design note:** bit-exactness alone could NEVER have caught this — arrival
order does not change the arithmetic, so a genuinely batched model would be
bit-exact and still misrepresent the hardware. That is why
`test_the_dies_are_concurrent_across_the_run` (both dies work on every trigger)
and `test_within_one_trigger_the_dies_are_causally_sequential` (the 99.9% figure,
pinned) are separate gates. The second one is deliberately written to FAIL if a
future change makes the crossing word appear early — that would be an improvement,
and it should be adopted deliberately rather than silently.

## fft_spectrum example SHIPPED — three LIVE-PATH defects that every headless gate passed 2026-08-24

The FFT blocks (16/32/64) were all verified bit-exact on chip and **none of them
had an example** — nothing to open, nothing to run. `examples/fft_spectrum` is
that example: `x16_in -> FFT -> ComplexToMagSquared -> x16_out`, so the
transform AND the per-bin power both run on the fabric, with the flowgraph
un-reversing the DIF bin order and plotting it. Two sizes ship as independent
`.kyt`/`.grc` pairs — **N=64** (84 FFT cells, 104/120 with routing) and **N=32**
(60 cells, 80/120).

**The whole value of this entry is that the headless gates were GREEN while the
user path was BROKEN, three separate times.** Each defect produced a *plausible*
spectrum. Anyone building the next chained example will hit at least one.

- **1. A hosted batch's I/Q lands on the CLIENT's default registers unless the
  stream is NAMED.** The GR client fills the `process_batch` header from its own
  `data_addrs=(0, 1)`; the server replaces that with the build-resolved landing
  **only** when the burst carries a `stream_id` present in
  `engine.port_config.stream_targets`. This chain lands on `[1, 2]`, so with no
  stream id the real part went to register 0 and the imaginary part to register
  1 — the block received a **real** input. A real signal's spectrum is
  conjugate-symmetric, so the tone split into two quarter-power peaks at bins 11
  and 53 (= 64−11): measured, two 6635 words where one 26539 was expected. It
  looks like a DSP bug and is an addressing bug. **Any single-stream chain whose
  landing is not `(0, 1)` needs a `stream_id` on the ingress net AND on both
  `.grc` markers** — `stream_targets` returning `{}` is the tell (the server
  prints it at start).
- **2. A repeat-burst source ROTATES the frame grid.** With `repeat = yes` the
  source re-arms mid-vector, so each later burst begins at an arbitrary rotation
  of the stimulus (this is documented behaviour — "samples arriving in between
  are consumed and dropped"). For a framed transform a rotation by `r` slides the
  frame boundary by `r mod N`: measured, a 55-sample rotation moved the peak from
  slot 52 to slot 43. Harmless for a memoryless or self-synchronising chain,
  **fatal for anything frame-aligned**. The fix is the CW/PSK31 shape — `repeat =
  no` on the source, `server_repeat = yes` on the sink — one genuine burst from
  index 0, looped for the display, with the gate asserting the loop is a
  byte-identical replay.
- **3. The host clips FULL-SCALE input.** `sim_bridge._float_to_q15` converts with
  `max(-1.0, min(0.999, f))`, so a sample at `32767/32768 = 0.99997` becomes word
  **32735**, not 32767. On a 255-sample full-scale burst that is 8 samples
  (indices 0, 48, 64, 112, 128, 176, 192, 240) — enough that a live run can never
  be bit-exact to a reference computed off-server. Any example that wants the
  user-path gate to demand **bit-exactness** (rather than a fuzzy tolerance) must
  drive below the clamp; this one uses amplitude 0.9, where the server's
  conversion and the example's reference agree on every sample.

**A CHIP-SCALE block cannot be auto-packed, and the failure is loud.** Asked to
pack FFT64, `auto_pnr` shifts the verified 12-row ctl/out spine off the array
(`block 'fft64' cell (2,12) is off the 10x12 array`) — the placer has no
CHIP_SCALE awareness at all (grep: no `CHIP_SCALE` anywhere under `placekyt/`).
The working shape is **pin the anchors, auto-route everything else**: place the
FFT at its own `default_layout()` anchor and the 1-cell power stage at `(9, 1)`,
then call the real `auto_route_all`. Two details are load-bearing and were
measured: the power stage's resting face must be **NORTH** (the default EAST
points off the array and the egress net dies with `no bus path from source to
the broker tap`), and the FFT→power link must be **ONE** connection —
`add_logical_connection` synthesises the Q-half sibling, and adding it by hand
too builds a duplicate net onto the same register that routes, builds, and then
emits **a frame of pure zeros with no error anywhere**.

**A userpath harness that "bound something" is more dangerous than one that
failed to bind.** During debugging `start_gnuradio_server` returned `None`
(no bind) while another session held 58950; the flowgraph then talked to *that*
server and the suite happily analysed **somebody else's chip output** (304215
words of a foreign stream, read as a spectrum defect). `_serve` now retries until
it holds 58950 **itself** and asserts the exclusive bind. Any new user-path suite
should copy that, not the bare `assert bound == _PORT`.

**Gates: 26.** The two user-path gates (one per size) host the shipped `.kyt` as
the GUI does and run the shipped `.grc` under the real GR interpreter; observed
N=64 `bin 11 at −0.9 dBFS, all 63 other bins −90.0`, N=32 the same at 32 bins.
Both sizes are also bit-exact on the real built chip (255/255 and 127/127 power
words) against the FFT's streaming reference composed with the power stage's.
Six mutations must FAIL, and the one that matters is **no un-reversal**: the raw
bit-reversed slots are a clean, plausible, *wrong* spectrum, which is exactly
what a display-layer gate has to catch.
## CSS receiver example — a `.grc` block param that is not a LITERAL builds the WRONG CHIP, silently 2026-08-24

The `examples/css_transceiver` example (CSS receive spine — dechirp → FFT16 →
|·|² → Delay(1) → BinArgmax — on one array, 82/120 cells) shipped end to end:
bit-exact vs the composed integer golden, SER 0 recovering `KYTTAR CSS` at
+10 dB, and an **on-chip** negative control at −10 dB in the same continuous
burst. Three durable lessons came out of building it.

- **THE ONE THAT COST THE DEBUG CYCLE — a `.grc` block param must be a LITERAL.**
  The flowgraph wrote `n: n_css` with `n_css = stim.N`, the same idiom every
  example uses for scope sizes. But the **placeKYT importer evaluates block
  parameters without the flowgraph's `stim` module** — that is a
  `from gnuradio.kyttar import …`, resolvable only in the GR interpreter. It
  does not raise: it falls back to the **yml default** (`n = 128`), so the chip
  was built for a 128-sample chirp while the host transmitted 16-sample chirps.
  Import ok, route ok, build ok, `auto_pnr.ok`, DRC clean, no route transits —
  and the chip emitted **6 garbage words instead of 50**, with values outside
  BinArgmax(16)'s legal `0..15`. The tell that finally cracked it was a
  bitstream diff against a hand-built chain with identical geometry and an
  identical net set: **5 differing words**, which decoded as the data constants
  `128,128,128` (argmax frame length) and `512` vs `4096` (the mixer's rate word
  `65536/n`). *Scope-sizing* variables may still be `stim.*` — GRC evaluates
  those and the importer never reads them. Only **block params** are affected.
  The example now carries a literal and a `_assert_chirp_len` guard that turns
  any future drift into a loud failure at import instead of a silent one on the
  chip. **Generalizes: any param the importer cannot evaluate degrades to the
  yml default rather than failing, so a stim-derived block param is a silent
  wrong-chip generator.**
- **A block can be orientation-invariant STANDALONE and still not compose.**
  A generic auto-place of this chain rotates the 44-cell FFT16 CCW and packs
  everything into the top nine rows; that layout routes and builds "ok" and
  does not work. Isolated: the *proven* geometry with FFT16 alone rotated CCW
  does not even complete a run (0 words, no quiescence) — while FFT16 passes
  `test_orientation_invariance.py` in all 8 D4 orientations **standalone**. The
  per-block orientation gate is necessary, not sufficient, for a large block
  inside a long chain. Handled the FSK4/QAM16 way: `build_kyt.py` imports the
  `.grc` for its topology and **pins** the proven anchors before routing.
- **A new OOT python module disarms the `grcc` smoke gate silently.** A demo
  stim module added to `gr-kyttar/python/kyttar/` is invisible to `grcc` until
  `install.sh` re-syncs it (which needs sudo), and every value that evaluates
  through it then fails to compile. `test_examples_grc_valid.py` already had a
  named skip for *stale installed ymls*; it now checks the installed **python
  modules** too (`_installed_kyttar_py_stale`), so this reads as the
  install-staleness condition it is rather than a bogus red on the flowgraph.
## gru_classifier — the example shipped BROKEN in the user's hands with a fully green suite; three faults, none visible to any existing gate 2026-08-24

Not a block. The shipped GRU-classifier example, reported broken by the project
owner doing exactly what the README says: open `gru_classifier.kyt` in placeKYT,
open `gru_classifier.grc` in GRC, press Run. The TRUE-class scope drew; the class
scope was flat; the waveform pane showed real I/Q at `x16_in`; the cell animation
showed data moving in the top rows and never entering the GRU. All 34 example
gates were green, including a headless on-chip run at agreement 1.000000.

**Reproduced first, through the real path** (host the shipped `.kyt` on 58950,
GRC-generate and run the shipped `.grc`): `sent 15360 samples -> 0 recovered`,
and the server's own startup line said `stream_targets resolved: {}` with every
input net's `stream_id` = None.

**Fault 1 — the `.kyt` carried no `stream_id`/`out_tag` (the example).**
`engine.port_config.stream_targets` resolves an input net's injection landing
ONLY for nets that carry a `stream_id`; `if not sid: continue`. With none, the
server fell back to `input_port_config`, which breaks on the FIRST `x16_in→block`
connection and returns ONE `data_addr`. This chain takes THREE nets off the port
(`in_re`, `in_im`, `in_zcr`), so the ZeroCrossingRate arm was never injected,
`FeaturePairJoin` never rendezvoused, and the GRU never ran — the owner's "data
never makes it into the array", precisely. Every other multi-arm example's `.kyt`
carried `stream_id` because it came from the GRC importer, which sets it; this
one is HAND-PLACED and its builder never did.

**Fault 2 — the live bridge could not drive a multi-arm COMPLEX stream (the
ENGINE).** Even correctly tagged, `sim_bridge._drive_one`'s fan-out branch was
`if s.get("landings") and xq is None:` — real-rail joins only — and the landings
tuple carried ONE address each. A complex stream fell through to the single
`(a0, a1)` path and drove one arm. Fixed: the branch is arity-driven, each
landing carries its whole address list, and the arm's own address count decides
its shape — **a 2-address landing takes the (Re, Im) pair as ONE delivery, a
1-address landing takes Re only**. That is the same INGRESS PROTOCOL
`gru_classifier.run_on_chip` had always hand-implemented; the server just never
learned it. The saturated `_stream_words` path got the same treatment.

Dedupe needed alongside it (`port_config`): a complex source into a complex block
is TWO port→block nets (`re`, `im`) that the router resolves to ALTERNATIVE
landings for the same block — the shared broker's two burst regs (`[1,2]`) and
the block's own input cell (`[0,1]`). They are one delivery, not two arms.
Driving both writes Im into the Re register (the bug this example already hit
once). Rule: **at most one landing per target block; keep the richest one.**

**Fault 3 — the `.grc` rescaled a RAW stream by ×32768 (the display).** A
complex-input chain returns RAW word floats (`output_words='auto'` ties raw to
`complex_in`), so the class index 0..3 already arrives as 0.0..3.0. The ×32768 —
the q15 convention, correct for REAL-input chains — drove every sample to
0/32768/65536/98304, far outside the scope's `[-0.5, 3.5]` axis. Correct chip
data, unreadable window. Even after faults 1 and 2 were fixed, the scope would
still have looked wrong.

**Bonus regression found on the way:** `examples/gru_classifier/gru_classifier.py`
— the CHAIN module (topology, anchors, `run_on_chip`, feature references) — had
been silently overwritten by the GRC-GENERATED flowgraph of the same name in an
unrelated commit. Every `from gru_classifier import ...` then hit
`ModuleNotFoundError: PyQt5` in the verification venv. Restored. Other examples
ship the generated `.py` beside a `<name>_demo.py`; here the names collide, so
this example does not ship the generated file.

**THE METHODOLOGICAL LESSON (the reason all this shipped).** The example was
gated to "the `.grc` opens / GRC-generates / instantiates" plus a headless
on-chip run — and the headless runner reads `input_landings` and drives the arms
ITSELF, so it never exercises the server's stream resolution, which is where all
three faults lived. **A headless on-chip gate does not imply a hosted gate.** The
shipping commit even said so in plain words ("NOT verified: the .grc has not been
run against a live hosted server") and it shipped anyway. If an example's
documented workflow is "open it in the GUI and press Run", the gate has to be
that, hosted, or the example is unverified — the example bar in `AGENTS.md` says
exactly this; it was simply not enforced by a gate.

Now gated by `test_examples_grc_userpath.py::test_gru_classifier_shipped_grc_user_path`
(demonstrated FAILING against the broken state before the fix; passing after,
480 words at agreement 1.000000, all four segment votes correct, clean
`server_repeat` repetition), plus two millisecond-cost structural guards in
`test_gru_classifier_example.py` so a regenerated `.kyt` losing its metadata or a
rescale creeping back into the `.grc` fails instantly instead of waiting on the
100s hosted gate.

**Audited for the same exposure:** every `examples/*/*.kyt`. Exactly one other has
≥2 input arms without `stream_id` — `coherent_bpsk_rx` — and it is NOT broken:
both its arms target the SAME complex block on registers 0 and 1, which is what
the fallback's hardcoded `a0, a1 = 0, 1` happens to be. Verified by running it
through the real hosted path: BER 0. It works by coincidence, not by resolution;
noted here so the next multi-arm hand-placed design does not read it as a
precedent.

**Harness note:** the userpath gates share port 58950 and the socket sits in
TIME_WAIT for 40-140s after each, so they self-contend under concurrent load
(and contend with any other agent hosting a chip). Run this gate STANDALONE; a
bind failure there is contention, not a product fault.
## FFT128 2-die: the "livelocked crossing" was the DRIVER — and the dies had been verified while the driver never was 2026-08-24

The N=128 two-die split was quarantined `needs_human` with *"0 of 520 words,
livelocks from trigger 1"*, after three real inter-chip build-path defects had
been found and fixed. **The crossing was innocent.** Wired together and driven
correctly the pair is **200/200 bit-exact** on the real two-chip system (73
non-zero outputs; then `examples/fft128_2die/`, since retargeted onto the real
2P2S board as `examples/fft128_2p2s/`). The remaining fault was in
the DRIVE, and the way it hid is the durable part.

- **A complex sample is a THREE-PART TRANSACTION, and on the multi-chip path
  each part must be pumped to quiescence before the next is injected.**
  `WRITE xi` → pump → `WRITE xq` → pump → `JUMP` → settle. Measured on the
  identical bitstream, 12 samples:

  | drive shape | words out |
  |---|---|
  | all three queued, one settle | **0** of 24 |
  | two WRITEs queued, then JUMP + settle | **0** of 24 |
  | one WRITE + one JUMP per WORD (the generic routed-head path) | 48 — **double-fires** |
  | WRITE, pump, WRITE, pump, JUMP, settle | **24** ✅ |

  Queued back to back, the single-outstanding input handshake is overrun and
  the system makes no forward progress — indistinguishable, from the outside,
  from a livelocked crossing. Note the server already had this right
  (`sim_bridge._process_batch_multichip` paces exactly this way); the
  investigation drove the engine directly and did not inherit it.

- **VERIFY THE PARTS SEPARATELY — and the DRIVER IS A PART.** The previous
  entry's decomposition (die 0 alone 80/80, die 1 alone 200/200, therefore the
  fault is the crossing) was sound reasoning from an incomplete parts list.
  Both single-die runs went through `run_block_dut_complex`, which paces
  correctly; the two-die run used a hand-written driver that did not. So the
  one component that differed between the working and failing configurations
  was never on the list of suspects. **When "each part works but the assembly
  doesn't", enumerate what the ASSEMBLY introduced — including the harness —
  not just the parts it joined.**

- **A LARGER BUDGET CAN HIDE THE BUG IT IS MEANT TO RULE OUT.**
  `run(events, rounds)` is events-per-chip-per-round × rounds. The original
  investigation reached for `run(400_000, 4000)`; re-measured here, that shape
  ran **over an hour on a 70-sample comparison** and had to be killed. It does
  not merely waste time — it lets one round churn arbitrarily far past the
  point where a missing pump would have been obvious, converting a crisp
  "nothing moved" into an ambiguous "still running". The shipped budgets are
  derived from what a sample has to do: `(60_000, 5)` per operand pump,
  `(200_000, 50)` for the settle, and 200 samples finish in minutes.

- **The crossing carries a PAIR and ONE trigger — verified, not assumed.**
  Watched at die 1's landing past die 0's delay-64 latency:
  `WRITE reg1 = out_i`, `WRITE reg2 = out_q`, `JUMP entry` — matching die 0's
  output stream word for word. The boundary is a transparent wire (die 0's
  exit cell emits both rails and the JUMP with the hop composed past the
  boundary), NOT a value relay that re-triggers per word. A per-word trigger
  would fire die 1 twice per sample on a half-primed operand pair — the
  on-chip "matched filter gets xi but never xq" data loss. The API surface
  invites the wrong conclusion here (`MultiChipSimulation` has no
  `write_port_multi_i16`, and the generic `MultiChipSimEngine.inject` really
  does WRITE+JUMP per word — which is why shape (3) above double-fires), so
  the packet shape is now pinned by a gate rather than inferred from the
  binding's method list.

- **The three earlier engine fixes were necessary.** They are what makes the
  paired delivery possible: the exit patcher no longer clobbers a mid-block
  exit's internal writes, both rails carry the cross-chip hop, and they land
  in consecutive registers. Without them the pacing alone would not have been
  enough. Landing proven fixes while stating plainly that the feature still
  did not work was the right call — this entry is what that honesty bought.

- **AND A SECOND, REPO-WIDE BUG FELL OUT OF GATING IT.** The example's
  "the shipped `.kyt` rebuilds to the verified bitstream" gate failed on
  roughly a coin-flip while the design was provably correct. Chasing that
  rather than loosening it found that **the build was not deterministic**:
  `cpsat_router` ran `num_search_workers = 8` with **no `random_seed`**, and
  its objective (minimise active cell-faces; sharing is free) has TIES by
  construction — so the parallel portfolio returned whichever equally-optimal
  routing its workers reached first. Measured: five builds, identical
  placement, occupancy, transit cells, faces and net order, and `_bfs`
  returning byte-identical results — yet **three distinct 17-cell routes** for
  the one net with tied optima, every other net stable. Every variant ran
  bit-exact on chip, so it was never a correctness bug; it made builds
  irreproducible and silently defeated any gate comparing bitstreams. Fixed at
  the source (`random_seed = 0` + `interleave_search = True`, which constrain
  WHICH optimum is returned and never how good it is): six builds now produce
  identical routes and identical bitstreams on both chips.
  **The generalisable part: when a gate is flaky, the flake is a finding.**
  The tempting move — weaken the assertion to "same size" and move on — would
  have left every CP-SAT-routed design in the repo irreproducible, and would
  have thrown away the only symptom that pointed at it. Also: *narrow the
  suspect by measuring inputs, not by reading code*. The router's own BFS
  looked deterministic and WAS; instrumenting its arguments proved the inputs
  were identical too, which is what eliminated placement, occupancy, net order
  and the spine in one step and left the solver as the only remaining variable.

- **AND A THIRD: a repo gate that was CHIP-BLIND, plus the quarantine it had
  manufactured.** `test_kyt_route_transits.py` built its cell-ownership map
  keyed on `(x, y)` with **no chip id**, collapsing every die onto one grid. On
  the first example with blocks on more than one die it reported a route on
  chip 0 "transiting" a block that is actually on chip 1 at the same
  coordinate. Fixing that revealed the more interesting half: **the `gain_2p2s`
  xfail was documenting the same bug.** Its recorded rationale — "the layout
  runs its tagged egress bus THROUGH gain_3 at (1,0) by original design" — was
  false. All three of its findings were cross-die (`gain_to_x16_out` is chip
  0's route; `gain_3` is on chip 3); each chip's egress crosses only its OWN
  gain, which is its endpoint and correctly excluded. Chip-aware, the example
  is clean and is gated normally, and `_KNOWN_OPEN` is now empty.
  **Two lessons.** *A quarantine is only worth its evidence* — this one had
  outlived its, and would have kept a correct example excluded from its gate
  indefinitely while reading as diligence. And *a false-positive fix can
  silently remove teeth*: keying on one GUESSED chip made the check pass
  everywhere, including where a genuine cross-chip net should still be
  examined, so the fix checks the route against BOTH endpoint chips and a
  companion test asserts the repo still contains an example of each shape
  (blocks on two dies; a net whose endpoints are on different dies) so neither
  the blind nor the guessed mistake can return unnoticed.

- **A NEW EXAMPLE IS A GATE-COVERAGE TEST, and this one found four gaps.**
  Beyond the two above, the repo's own ratchets flagged the new design twice,
  and both were worth fixing properly rather than waiving:
  (a) **Route quality.** Die 0's egress is +10 over manhattan, past the global
  `MAX_NET_EXCESS` of 8. It is not wander — die 0's fold walls off rows 0-3
  across columns 2-8, boxing the egress cell at (2,1) in, so every route must
  escape west, drop to the free row 4, cross, and climb column 9. The tempting
  fix (raise the global cap) would blind the ratchet for *every other* design.
  Instead: a per-net `WALLED_IN_NETS` exception that the gate **proves** —
  `test_walled_in_nets_really_are_shortest` flood-fills the free cells and
  fails if a shorter path exists, or if the net later becomes short enough that
  the exception is no longer needed. **An exception that checks its own premise
  cannot rot into a licence.**
  (b) **Saturation coverage.** Both dies needed a `NEEDS_BESPOKE` reason: the
  shared harness drives a block alone on one chip, and half a transform is only
  meaningful as half of a pair (die 1's input is die 0's *output*, not a raw
  signal). The bespoke gate is the stronger one — it asserts bit-exactness, the
  per-trigger rate AND quiescence on the real two-chip system.
  (c) INV-38 caught this example's own report writer hardcoding
  `"passed": True`. The guard was right; the verdict is the session's.

- **WHEN A SUITE REPORTS 120 FAILURES, FIND THE ONE CAUSE BEFORE FIXING 120
  THINGS.** A full run came back with 120 failures, nearly all
  `test_emit_report` / `test_write_report`. Every one passed in isolation. The
  mechanism is by design: `write_session_report` refuses to write if ANY gate
  failed in the session, so a single genuinely-broken test poisons every report
  writer downstream of it. Here the seed was 8 errors in an example that
  imports `PyQt5` (absent from this venv, which has PySide6) — **pre-existing,
  untouched by the work, and nothing to do with the 112 blocks that "failed"**.
  Re-running with the report writers deselected cut the noise to 11 real
  failures, of which exactly two were mine. Two habits fall out: *a cascade has
  a shape* (all failures in one test-NAME, all passing alone), and *the way to
  see past it is to deselect the cascading layer*, not to start fixing its
  victims.

- **Ship the vehicle, not just the verdict.** The two-chip driver that
  produced the original "0 of 520 words" measurement was never committed, so
  the next person started from prose. The two-die example then shipped the
  `.kyt`, the `.grc`, and a demo that reports per-trigger yield, the crossing's
  traffic and the first non-quiescent trigger — plus `--pattern batched`,
  which reproduced the failure on demand so the trap stayed demonstrable
  instead of becoming folklore. (That example has since been retargeted onto
  the real 2P2S board as `examples/fft128_2p2s/`, which carries the `.kyt`,
  the `.grc` and the demo but not the `--pattern batched` reproducer.)

## Verification-integrity sweep — every report writer in the suite could write a GREEN report for a FAILING session (INV-36) 2026-08-24

Not a block. A repo-wide audit of the code that produces the project's evidence,
triggered by a real instance: an FFT64 builder found that its own
`test_zz_write_report` hardcoded `"passed": true`, so a session whose
saturated-drive gate FAILED still emitted a green
`verification/reports/FFT64Block.json`. It self-reported and fixed its own writer.
This sweep asked the obvious follow-up — *how many others?* — and the answer was
**every one of them, by one route or another.**

- **THE SCOPE, MEASURED, NOT ESTIMATED.** ~100 report-writing functions across 84
  files. An AST scan of the pre-fix tree classifies them into exactly three
  shapes, and the guard test now fires on all three (proven by running it against
  the pre-fix sources: **30 findings across 17 files**; against the fixed tree,
  zero):
  * **17 writers hardcoded the verdict** — 14 as a literal `"passed": True` in the
    payload dict, 4 more as a *fabricated* `CompareResult(passed=True, ...)` handed
    to the shared `write_report` (`GRUCellBlock`, `QAM16ComplexCostasLoopBlock`,
    `RaisedCosineEnvelopeBlock`, `TwiddleMultiplyBlock`). The fabricated shape is
    the dangerous one: the call site reads as correct, because `write_report` *does*
    derive `passed` from `result.passed` — it just derived it from an invented result.
  * **~83 writers derived their verdict honestly** from a real `CompareResult` — and
    were still unsafe, for the reason below.
- **"DERIVES ITS VERDICT FROM A REAL COMPARISON" IS NOT ENOUGH.** This was the
  finding that changed the shape of the fix. pytest continues past a failure by
  default, so a `test_emit_report` that re-runs one comparison and asserts it
  passes will happily run — and write — in a session where the *mutation*,
  *orientation*, or *saturation* gate for that same block failed. A per-comparison
  verdict is not a session verdict. Fixing only the 17 hardcoders would have left
  the other 83 able to certify a block whose real gates were red.
- **AND A REPORT NOBODY WRITES IS STILL A CLAIM.** The stalest shape of all: a
  green report left on disk by an earlier passing session, which a later session
  that *crashed, was killed, or failed* never removed. The file goes on attesting
  to a state of the code that was never verified. Hence the load-bearing ordering:
  **unlink FIRST, before the verdict is even known.** Absence is the safe state.
- **THE FIX IS ONE HELPER, NOT 100 PATCHES.** `kyttar_verify/session_report.py`
  implements unlink-first + a zero-failures/zero-errors session gate; the shared
  `write_report` routes through it (covering the ~83 in one edit) and the 17
  hand-rolled writers were converted to call it directly. Nothing asserted by any
  suite changed — only *whether and when* a file appears.
- **PUT THE SESSION RECORD ON THE PYTEST `Config`, NEVER IN A MODULE GLOBAL.** A
  global is precisely what a crashed run leaves stale, which defeats the whole
  point. `Config` is per-session and per-process: safe under parallel invocation,
  impossible to inherit across processes, and *missing* rather than wrong when the
  conftest plugin is absent — in which case the writer refuses. There is no
  "assume it passed" branch anywhere in the module, deliberately.
- **QUARANTINE REPORTS NEEDED AN EXPLICIT `verdict=False`.** Routing everything
  through a helper that stamps `passed: True` would have silently flipped
  `GardnerTimingRecovery`'s honest quarantine record (`passed: False`, a block that
  demonstrably does not work) into a green one — the same defect, introduced *by
  the fix for the defect*. Caught in review of the mechanical conversion diff. The
  session gate still applies in full, so `verdict` can only make a record worse
  than its session, never better.
- **THE GATE ON THE GATE (INV-4, applied to the writer).** A writer never shown to
  REFUSE certifies nothing. `test_report_provenance.py` runs a real writer in a
  child pytest session containing a synthetic FAILING test and asserts no file
  appears and the writer fails — plus a passing control (a writer that never writes
  would trivially satisfy "no file"), an unlink-first case, `-x`, `-p no:randomly`,
  and a genuinely concurrent parallel case. The repo-wide guard has its own teeth:
  all three defect shapes are reintroduced in a fixture tree and proven to fire,
  with a negative control proving the guard stays silent on a correct writer.
- **THE PROVENANCE PROBLEM IS NOT RETROACTIVELY SOLVABLE.** The mechanism makes
  every *future* report trustworthy; it cannot retro-certify the 110 already on
  disk. Those written by a hardcoding writer are unverifiable **by provenance** —
  which is not the same as wrong, and most are certainly fine. The honest move is
  to name them (see the audit in the accompanying report) and re-run their suites,
  not to delete them wholesale and not to quietly keep trusting them.
## gru_classifier example SHIPPED — a WIDE-FLAT (chip-scale) fold turned "one net short" into 102/120 2026-08-24

The classifier chain had never routed on one 10x12 across four dispatches and
~8200 measured layouts; the best result was always exactly ONE failing net, with
WHICH net failed rotating as blocks moved. It routes now, builds at **102/120**,
and classifies the shipped stimulus on the real chip at **agreement 1.000000**
against the offline chip-exact golden. The fix was one method on `GRUCellBlock`.

- **THE DIAGNOSIS THAT WAS RIGHT, AND THE CONCLUSION THAT WAS TOO NARROW.** The
  previous dispatch measured the wall carefully and ruled out capacity (65/120
  block cells), the hop ceiling (INV-36; no `hop_overflow` in 5039 layouts) and
  the arm (boxcar 32/16/8/4 with and without Sqrt: 65 cells -> 4 short, 62 -> 2,
  57 -> 1, 56 -> 1). It then argued no FOLD could close it either, from a sound
  structural fact: **a closed ring can never contain a free through-channel** (a
  cycle cannot jump a gap), so all of its free space is perimeter — and
  free-space quality measured IDENTICAL across every legal fold. The fact is
  true. The conclusion silently inherited INV-9's <= 8-across cap, under which a
  51-cell block has only THREE legal bounding boxes. **Waive the cap and the
  perimeter of a 10-wide block IS six contiguous full-width rows.** Lesson: when
  a proof says "no X can do Y", check what quantified the space of X.

- **THE FOLD.** 50 ring cells as a closed row-comb Hamiltonian cycle over a 10x5
  box (fully covered, no holes, no enclosed interior), re-indexed so `fin` lands
  at the head row's west end and `amx` three cells along it, with the off-ring
  `oout` relay one row above. 10 wide x 6 tall. Chosen by enumerating the
  closed-cycle comb/row-comb families at every cell-count-dividing box within the
  chip-scale caps x every start-and-direction re-indexing x every free relay
  slot, keeping only candidates satisfying EVERY rule — closed cycle, bbox in
  caps, no enclosed interior, a free relay slot at `amx`, the ROUTE-TIME FACE
  RULE over the block's real `internal_connections`, every ring distance <= 31 —
  and ranking the **24 survivors** by port cost then contiguous free rows.

- **`CHIP_SCALE` IS A TRADE, NOT A LOOSENING.** The flag is declared per class
  (`_base.py`'s existing machinery, as FFT32/FFT64 use it) and buys width in
  exchange for a hard obligation: nothing can reach the far side of a 10-wide
  block, so **its input and output must share ONE edge**. `fin` and `oout` are
  three cells apart on the north edge, facing the chip's two row-0 ports.

- **THE WIDE FOLD IS NOT CHEAPER FOR ITS OWN CORRIDORS — and saying so matters.**
  At its BEST anchor (row 0) the block plus its two port corridors builds in 58
  cells, against 64 for the 8x7. But the example seats it at row 6 (**70** cells;
  the cost rises +2 per row of descent) precisely so the front end gets the six
  port-side rows. **The fold wins on the SHAPE of the free space it leaves, not
  on its own cost.** A gate that measured only the block's port cost at the
  example's anchor would have read this as a regression and been right about the
  number and wrong about the outcome; it is now stated at the best anchor with
  the trade documented beside it.

- **BEHAVIOUR PRESERVED EXACTLY, and INV-37 is why it was a one-method change.**
  All 49 `test_gru_cell.py` gates green: 36,000 on-chip steps at agreement
  1.000000, clip vote 0.9667 on-chip == 0.9667 offline over 120 held-out clips.
  The three `is_face` constants all MOVED (`fin` LOCK_FACE 2->1, `amx` faces
  (2,3)->(3,2)) and followed automatically because the previous dispatch had
  already derived them from the fold. Re-folding a block whose faces are derived
  costs one method; with them baked it is a silent-garbage trap.

- **A CHIP-SCALE BLOCK MUST STILL BE ORIENTATION-GATED, SOMEWHERE.** It cannot
  run the shared full-D4 sweep (a full-width fold has no room to rotate), so it
  is removed from `test_orientation_invariance.py` and gated on its DECLARED
  `CHIP_SCALE_ORIENTATIONS` in its own suite (the FFT32 pattern). That leaves an
  obvious hole — drop it from the shared list, forget the per-block gate, and it
  is gated NOWHERE while everything stays green. New
  `test_chip_scale_blocks_are_gated_elsewhere.py` closes it: every chip-scale
  class must name the suite that gates it or be listed as quarantined, and a
  quarantined one that reaches manifest-`done` fails.

- **THE EXAMPLE'S REAL BUG WAS IN THE DRIVER, NOT THE CHIP: a complex pair is ONE
  delivery.** With the chain routed, the first end-to-end run classified 9/12
  instead of exactly. `in_re` and `in_im` share ONE corridor ending at a BROKER
  one cell short of the power cell, and `in_re`'s landing carries BOTH staging
  registers in its `data_addrs`; `in_im`'s landing describes the FINAL
  destination. Driving them as two independent deliveries wrote Im into the power
  cell's **Re** register: `im` stayed 0, power silently became `re^2`, and every
  downstream stage still looked plausible (the sqrt trio, the decimator and the
  ZCR arm were all individually correct). **When a complex consumer is fed from a
  port, drive the BROKER landing, not the per-rail one** — and read the landings,
  do not assume the rail count. Now an on-chip mutation gate.

- **THE WIDE FOLD COSTS THE FRONT END SOME ROUTE QUALITY, and the ratchet said
  so.** `test_route_quality.py` failed the new `.kyt` at +6 total excess against
  a new file's implicit budget of 0. Both detours are the placement-forced
  wall-detour class and are a DIRECT consequence of the fold: with the GRU
  holding rows 6-11 across the full width, the whole front end is confined to
  rows 1-4, so `pow_mean` rounds the boxcar's 2x4 footprint (+2) and
  `root_decim` cannot run straight along row 2 (walled by sqrt/boxcar cells at
  (6,2),(7,2),(8,2)) and drops to the free row 5 and back (+4). Pinned
  CONSCIOUSLY with that explanation, per the ratchet's own instruction — the
  placement is not free to improve, since a 400-layout search found exactly ONE
  arrangement that routes AND builds. Worth recording as a cost of the trade:
  buying a contiguous through-channel for the big block spends some of the small
  blocks' lane freedom.

- **THE EXAMPLE.** 34 gates. On-chip: 480 windows, agreement 1.000000, segment
  votes [0,1,2,3], per-step accuracy ssb 1.000 / bpsk 0.811 / fsk4 0.856 / noise
  1.000, asserted EQUAL to the offline model's on the same clip. Ships `.kyt` +
  `.grc` (port 58950, scopes sized below the burst) + golden + demo + a
  self-contained `kyttar.gru_demo_stim` whose clip is asserted identical to
  `gru_stimulus.py`. Hand-placed, like FSK4/QAM16: a 400-layout search found
  exactly ONE arrangement that routes AND builds, so **open the `.kyt`, do not
  import the `.grc`**.

- **THE FUSED FEATURE BLOCK WAS NOT NEEDED.** The standing recommendation was to
  fuse the six front-end blocks into one `FeatureExtractorBlock` to delete six of
  the ten nets (estimated ~99/120). It was designed (14 cells, a 7x2 fold with
  both external cells on the port-facing edge) and abandoned unbuilt once the
  wide fold routed the ORIGINAL six-block chain at 102/120. Recorded because the
  net-count arithmetic behind it is still sound and is the right lever if a
  future chain saturates the array again — but the cheaper lever is to ask
  whether a dominant block's FREE SPACE has the right shape.

## GRUCellBlock RE-FOLD — a baked `is_face` literal PINS a fold, and the classifier's wall is corridor BUDGET, not fold shape 2026-08-24

Dispatched to re-fold `GRUCellBlock` so the gru_classifier front end could route
beside it on one 10x12. The fold moved and measurably improved; the wall did not
fall. Both results are worth more than the one that was asked for.

- **THE REAL BUG THE RE-FOLD FOUND (now INV-37).** Three `is_face=True` data words
  — `fin`'s `LOCK_FACE`, `amx`'s `face_out`/`face_ring` — were LITERALS matching the
  as-authored fold. Every re-fold tried (a solid 5x10 Hamiltonian ring, the
  transpose, a reversed traversal of the same ring) produced a perfectly legal
  layout — closed cycle, in-cap bbox, clean route-time face rule, right dict order
  — that BUILT and then computed garbage: 20 of 52 gates failed, the recurrence
  never landed, `h` froze at its timestep-0 value, and the sim ran to `EventLimit`.
  The geometry gates cannot see this because the geometry is fine. Deriving the
  three words from the fold (`_face_from(a, b)` over the ring positions) made the
  SAME transposed fold pass all 52 unchanged. **A closed ring is direction-free for
  @N distances but NOT for faces** — reversing the traversal reverses every resting
  face, which is why "it's just a relabelling" is wrong for any block that MOVEs a
  literal into `[FACE]`/`[LOCK_FACE]`.

- **THE FOLD THAT SHIPPED: 8x7, the landed serpentine TRANSPOSED.** Chosen by
  exhaustive search over the comb / row-comb / spine-snake closed-cycle families x
  8 D4 images x 100 start-and-direction pairs, filtered on every fold rule and
  ranked by PORT COST — min over anchors of `|fin - x16_in| + |oout - x16_out|`,
  which matters because the 10x12's two 16-bit ports are BOTH on row 0. The old
  7x8 put the input on the north-west corner and buried the egress two rows down
  the WEST edge, pointing away from the output port: cost 11, and `gru_out` alone
  measured 15-17 corridor cells. The transpose spans the north edge (fin (0,0),
  oout (2,0)): cost 7, and five free ROWS instead of three free columns. On the
  identical lane search the old fold bottomed out 2 nets short, the new one 1.
  BEHAVIOUR IS PRESERVED EXACTLY, re-verified on chip after the re-fold: 36,000
  on-chip steps at agreement 1.000000 against the golden, clip vote 0.9667
  on-chip == 0.9667 offline over 120 held-out clips, all 53 gates green.

- **BUT THE FOLD IS NOT THE LEVER, AND NEITHER IS THE ARM.** Under INV-9's 8x8 D4
  cap a 51-cell block has only three possible bounding boxes (7x8, 8x7, 8x8), and a
  CLOSED RING can never contain a free through-channel — a cycle cannot jump a gap,
  so all its free space is perimeter. Free-space quality therefore measured
  IDENTICAL (29 3x3-anchors, 48 2x2) for every legal fold; only port proximity
  varies. Shrinking the RMS arm was swept too: 65 block cells -> 4 nets short,
  62 -> 2, 57 -> 1, 56 -> 1. **The wall is the ten nets' corridor budget.** Measured:
  65 block cells leave 55 for routing; the tail's six nets already cost 51 (8.5/net),
  so ten nets want ~85. 4180 further layouts on the re-folded block stayed at
  exactly one net short.

- **NEXT LEVER, NAMED.** Not the router, not the fold, not the boxcar length: FEWER
  SEPARATELY PLACED BLOCKS. One fused feature block in place of the four-block RMS
  arm removes block cells *and three of the ten nets* — and nets, not cells, are
  what the array has run out of. Otherwise, a two-chip topology.
## VERIFY THE PARTS SEPARATELY — how a 2-die split localised its own failure, and 3 engine defects the shipped 2-chip example could never expose 2026-08-24

Splitting the N=128 FFT across two dies produced a system that placed, routed and
built on both chips and emitted NOTHING. What made that tractable was refusing to
debug it as one thing.

- **DECOMPOSE BEFORE DIAGNOSING.** A feed-forward 2-die design has exactly three
  independently checkable parts: die 0, die 1, and the CROSSING. Each die was built
  ALONE on one chip (`x16_in -> die -> x16_out`) and driven:
  die 0 → **80/80 bit-exact** (16 non-zero outputs past its delay-64 latency);
  die 1 → **200/200 bit-exact** when fed *die 0's own output stream*, which is what
  it sees in service. Both halves correct ⇒ the fault is the crossing, established by
  measurement in two cheap single-chip runs instead of inferred from a dead system.
  **Feed each stage the stream it actually receives**, not the raw input — otherwise a
  passing stage-2 gate says nothing about the assembled chain.

- **THEN DIFF THE SAME BLOCK BUILT BOTH WAYS.** The decisive evidence was building
  the IDENTICAL die for one chip and for two and comparing its exit cell:
  ```
  one chip  : WRITE @1 x3 (feedback pair + lock-clear) + WRITE @19 x2 + JUMP @19
  two chips : ALL FIVE at @23
  ```
  Same block, same anchor, same cell — so the difference is the build path, not the
  design. That diff is worth reaching for whenever a component works in one context
  and not another.

- **THREE ENGINE DEFECTS, all invisible to the shipped example.** `_patch_cell_handoff`
  rewrote EVERY WRITE/JUMP in the exit cell to the cross-chip hop, so a block whose
  exit cell also carries internal handoffs lost them: an R2SDF stage's `out` writes
  its emerging pair back into its own `ctl` and clears the stage's serialize-LOCK at
  @1 before emitting. Patching only the LAST write is also wrong (a complex exit emits
  out_i THEN out_q and both rails need the hop), and both rails were steered to
  `in_regs[0]` when a complex packet's rails go to CONSECUTIVE registers. The
  single-chip Router had honoured all of this for as long as `output_at_last_write`
  existed; the inter-chip path never had. **It hid because the only shipped 2-chip
  example is a GainBlock, whose exit cell emits nothing but its output write — there
  was nothing to clobber.** An engine path exercised by exactly one trivial example is
  untested for every non-trivial one.

- **FIXING WHAT YOU PROVED IS NOT THE SAME AS FIXING THE BUG.** All three defects are
  real, demonstrated, and fixed (27 multichip tests still pass). The 2-die FFT still
  livelocks. The commit says so: the exit cell is now provably byte-identical to its
  working single-chip form, and at least one further fault remains. Landing three
  proven fixes while stating plainly that the feature does not work is the honest
  outcome — the alternative, quietly implying the last fix was the fix, is how a
  broken feature ships.

## MERGING TWO AGENTS' WORK ON ONE PLANNER — a fallback inside a backtracking search is an EXPONENTIAL, and a layout hash is the only proof a merge is safe 2026-08-24

FFT32 and FFT64 were built in parallel against the same `fft_large.py`. Both hit the
SAME `s1_fetch_d` INV-33 overlap at the P=16 boundary and fixed it differently; both
improved the spine planner. Merging them surfaced two lessons worth more than the
merge itself.

- **KEEP THE ROOMIER FIX, NOT YOUR OWN.** FFT64's fix moved `fetch_d`'s `c` forward to
  the accumulator-delivery idiom (31/32 words); FFT32's REMOVED the cross-forward
  outright so each table cell writes straight into `steer`'s input register (30/32,
  two clear). Both are correct and both were separately verified, but shipping two
  shapes of one builder invites divergence between sizes. The merge kept FFT32's and
  deleted FFT64's: **one `_fetch_cell`, the roomier shape, no per-size branch.** When
  two agents fix one defect, the deliverable is ONE fix — pick on margin and
  simplicity, not on authorship.

- **A FALLBACK PLACED INSIDE A BACKTRACKING SEARCH MULTIPLIES THE TREE.** FFT64's
  planner gained a height-capped fallback so long chains over short spines stop
  rediscovering the same wall. It was implemented as a generator yielding SEVERAL
  candidate batches per stage — which is fine in isolation and catastrophic in
  recursion: the generator is consulted at EVERY level, so k batches per stage
  multiplies the search by `k**n_stages`. Measured on the merge: FFT32's sub-second
  solve had not finished in MINUTES, while its layout was perfectly acceptable
  (verified directly — main's chip-proven FFT32 fold PASSES the new corridor check, so
  the check was not the problem). Fix: make the fallback a **whole-search retry**
  outside the recursion — pass 1 is exactly the original single full-height batch, and
  only if that finds nothing does pass 2 re-run the entire search with a capped board.
  Cost for any already-placing size: zero. **A fallback must be reachable only after
  the original path has fully failed, and "fully" means the whole search, not one
  node of it.**

- **THE LAYOUT HASH IS THE MERGE GATE.** A planner merge is safe only if every
  already-verified size produces a BIT-IDENTICAL layout — not "still places", not
  "still passes its structural gates". A different working layout silently invalidates
  every on-chip measurement taken against the old one, and nothing in the test suite
  would say so. Hash it and pin it:
  `sha256(json.dumps(sorted(layout.items()), sort_keys=True))`. FFT64 held at
  `e27f020b27441656` through five corridor-check changes, a search-order change, and
  this merge; FFT32's fold came back byte-identical to main's footprint. Without those
  two numbers the merge would have been indistinguishable from a silent regression —
  which is exactly the failure class this campaign exists to eliminate.

## A REPORT THAT HARDCODES ITS OWN VERDICT, and a shared EVENT CAP that calls a healthy block livelocked 2026-08-24

Two test-side defects found in one FFT64 run, both of which would have shipped a
false result. Neither is FFT-specific.

- **NEVER HARDCODE `"passed": true` IN A REPORT.** The FFT64 report writer emitted
  `"passed": true` unconditionally. On the run where the saturated gate FAILED, it
  still wrote a green report — the dashboard would have read a pass that did not
  happen. This is the "make the gate look green" failure the project exists to
  eliminate, and it was in freshly written test code, so nobody is immune.
  **The fix has two halves and both matter:** (1) the report file is UNLINKED at the
  start of the writer, so a failing *or dying* session cannot leave a stale green file
  — **absence is the safe state**, and the dashboard reads absence as "not verified";
  (2) the write happens only when the session reports zero failures, read from
  pytest's own `terminalreporter` stats rather than from bookkeeping the module keeps
  itself. Prove it the same way it was proven here: run the writer in a session
  containing a synthetic failing test and assert it refuses, fails, and leaves NO file.

- **A SHARED EVENT CAP IS A BLOCK-SIZE ASSUMPTION IN DISGUISE.**
  `run_block_dut_pipelined` caps a saturated run at `max(50_000, 2_000 * n_samples)`
  and reports expiry as *"block livelocks when the pipeline is full"*. That default is
  sized for small blocks. FFT64 is 84 cells over six serialize-LOCKed stages and
  **measured 2873 events per sample on chip** (8 consecutive samples, range
  2727..2969), so a 127-sample burst needs ~365k events before any margin — against a
  254k cap. The harness reported a LIVELOCK, the block was healthy, and re-running the
  identical burst with a real budget completed and matched the golden **254/254 words
  bit-exact**, all six locks releasing correctly.
  **The trap is that the message names a conclusion, not a symptom.** "Livelocks when
  the pipeline is full" reads as a diagnosis, and it cost a commit asserting a defect
  that did not exist. Distinguish the two cheaply and always: **measure the per-sample
  event cost on the per-sample path first**, then compare against the cap. A genuine
  livelock never reaches quiescence at ANY budget; a cap shortfall completes as soon as
  the budget is real.
  **Raising a cap is only legitimate when the new number is DERIVED.** Record the
  measurement next to the constant (here: measured cost x2 as a ceiling on a bounded
  run), so it is visible as an arithmetic budget rather than a number enlarged until
  the gate went green. A cap tuned to pass is a loosened tolerance wearing a different
  hat.

## THE PROXY-GATE FAILURE MODE — a cheap structural check that PASSES where the real thing FAILS 2026-08-24

**The general rule, first, because this is not about FFTs.** A structural pre-check
that stands in for a real engine — a router, a build, a scheduler — must model that
engine's **sequencing, its termination condition, AND its resource bounds**. Miss any
one and it is a PROXY: it will pass placements/designs the real engine rejects, and
every hour spent trusting it is spent debugging the wrong thing. **When a pre-check
and the real engine disagree, the real engine is right and the CHECK is the bug.**
Fix the check; never widen the engine to match a proxy, and never conclude "no
solution exists" from a proxy's verdict.

This failure mode has now cost this campaign three separate investigations (twice in
the FFT64/128 work, once in an example that reported `.ok` on every flag while
emitting garbage), which is why it is written up on its own.

**The concrete instance.** `LargeFFTBlock._corridors_ok` asked "is some NEIGHBOUR of
each landing 4-connected to a port?", per net, independently. The real pipeline does
FIVE things that check did not model, and each one alone is enough to make it lie.
They were found ONE AT A TIME, each by a routing failure the previous fix exposed —
which is itself the lesson: a proxy usually hides more than one discrepancy, so keep
re-running the REAL engine after each fix instead of assuming the last one was the
last one.

1. **Nets are routed SEQUENTIALLY WITH SHARED OCCUPANCY.** The router lays one net at
   a time and RESERVES each finished corridor against the next. Two nets that are each
   individually routable can therefore still collide. Measured order matters: the
   egress net routes FIRST and consumes cells, and only then does the ingress net try.
   The FFT128 die-0 placement passed the independent check and then failed for real
   with `no free corridor between the ports`.
2. **A corridor ends ON the landing cell, not beside it.** The router's BFS walks free
   cells and terminates AT the destination. Asking only whether a NEIGHBOUR of the
   landing is reachable passes a placement whose landing is walled in — the corridor
   can reach the neighbourhood and still have nowhere to finish.
3. **Hop count is BOUNDED at 31** (a 5-bit HOP_CNT), and an egress corridor spends one
   extra hop leaving the array. A path can be perfectly connected and still
   unroutable: the die-0 detour around a tall fold measured 26 hops of pure corridor
   before the block's own internal hops were counted. Connectivity is not routability.
4. **The ORDER of the nets is the CALLER'S, not the block's.** Nets are routed in
   project connection order — whatever the caller happened to add first — and the two
   orders are not equivalent, because each finished corridor is reserved against the
   next. A placement validated for one order can fail on the other: the die-0 plan
   passed a check assuming egress-first and then failed for real on `mid0` when the
   caller wired ingress first. **Require BOTH orders to succeed**, or a placement is a
   latent failure waiting for a caller to wire its nets the other way round.
5. **THE PLACER NORMALISES A LAYOUT TO ITS OWN BOUNDING BOX, and that invalidates
   every absolute fact the other four rest on.** `place_block(x, y)` emits each cell
   at `x + dx - min_dx` — it TRANSLATES the footprint so its bounding-box minimum sits
   at the anchor. A plan whose cells start at `min x = 1` therefore reaches the router
   SHIFTED ONE COLUMN LEFT when anchored at 0, and every port distance, reserved lane
   and free column the planner reasoned about describes a layout that no longer
   exists. Measured: the N=128 die-0 plan sat at `min x = 1`, passed the corridor
   check, and arrived at the router with block cells on (0,2) and (0,3), sealing the
   input port.

   **The fix is a DECLARED ANCHOR, not a rejection.** The first attempt here was to
   reject any plan not already normalised — that was wrong, and measuring it showed
   why: die 0 *has* to reach column 1 to leave its corridors open, so `min x = 0` is
   unreachable for it, and the rule threw away the only valid geometry (die 0 failed
   to place at all; die 1, which satisfies it by luck, was fine). Instead the block
   DECLARES the anchor at which its plan is reproduced verbatim —
   `default_anchor = (min_dx, min_dy)` — and callers place it there, which makes
   `x + dx - min_dx == dx`, the identity. What the router sees is then exactly what
   was validated.

   The general form: **if a planner reasons in absolute coordinates, the placement API
   must be told where those coordinates are valid.** A constraint that forces the plan
   into the API's default frame is the wrong direction of fix — it discards geometry
   to satisfy a convention. Note also why this hid for so long: the shipped N=64 fold
   happens to touch x=0 and y=0, so anchoring was a no-op for it and the whole issue
   was invisible until a SECOND size existed. A contract exercised by exactly one
   instance is not yet a contract.

`_corridors_ok` now mirrors all three — egress first, then ingress over what the
egress left, both terminating ON their landing cells, both hop-bounded. Under the
corrected check the placement that the router had rejected **failed honestly**, which
is the confirmation that matters: the check and the engine now agree.

**LAYOUT-HASH STABILITY — the regression pattern for ANY planner change.** A placer
change is not safe because the block "still places". Hash the resolved layout and
assert it is UNCHANGED for every already-verified size:

```python
hashlib.sha256(json.dumps(sorted(lay.items()), sort_keys=True).encode()).hexdigest()
```

The shipped FFT64 fold held at `e27f020b27441656` (spine column 4, 84 cells,
`s0_ctl (4,0,east)`, `s5_out (4,11,east)`) across BOTH the stricter corridor check and
the new fallback search. That single number is what licenses a planner change against
an already-verified block — "it still works" does not, because a DIFFERENT working
layout silently invalidates every on-chip measurement taken against the old one.

**The height-capped fallback** (see the entry below for why it was needed) is ordered
so this stays true: **the full-height batch is tried FIRST, always**, so a size that
already places never generates the extra batches — it pays nothing in time and gets a
bit-identical layout. Only when full-height yields no placement does the search fall
back to shrunk boards, shortest viable fold first. Design fallbacks this way: the new
path must be unreachable for inputs the old path already handled.

## ENGINE FIX (not block work) — the spine planner enumerated PORT-BLIND, so 400 candidate folds all sealed the egress 2026-08-24

Found while placing the N=128 die-0 half, but it is a **placement-engine defect any
large block would hit**, so it is recorded on its own rather than buried in a block
entry. Nothing about it is FFT-specific.

- **The mechanism: a post-hoc check can REJECT but cannot STEER.**
  `_corridors_ok` — "can the router still reach this fold's I/O from the chip
  ports?" — runs only after EVERY stage has been placed. The candidate generator
  (`_self_avoiding_paths`) is a fixed-order DFS returning at most
  `_SPINE_PATH_CANDIDATES` (400) walks, and it knows nothing about the ports. For a
  LONG chain over a SHORT spine those 400 walks are not 400 different shapes: they
  are 400 minor variations of ONE shape, a tall wall on one side of the spine
  spanning every row. Sorting that sample cannot rescue it, because every member of
  the sample is bad in the same way.

- **The measurement.** Die 0's stage-0 chain is 30 cells over a 2-row spine on the
  10x12. All 400 enumerated candidates ran WEST of the spine column across all 12
  rows; **0 of 400 passed the corridor check**. Bounding boxes: 210 spanned cols
  1..5 rows 0..11, 188 spanned cols 2..5 rows 0..11, 2 spanned cols 3..5 rows 0..11.
  The asymmetry is the tell — the INPUT corridor was reachable in every case and the
  OUTPUT corridor in none, because the wall lands between the block exit and the
  x16_out port. A 30-cell walk from `(4,0)` to `(4,1)` demonstrably EXISTS (the
  enumerator finds them instantly with the blockage removed), so this was never an
  infeasible geometry — only an unlucky enumeration order.

- **The fix: port-aware enumeration with a progressively reserved egress lane.** A
  column between the spine and the output port may be RESERVED — no stage cell may
  occupy it — which guarantees a free north-south lane for the egress corridor AND
  forces the enumerator to spend its 400 candidates on walks that leave it alone.
  Reservations are tried in order `None` first, then each column east of the spine,
  nearest first, so the change can only ADD solutions. Effect on the failing case:
  stage 0 went from "no solution at any of 8 spine columns" to placing in **0.2 s**
  with column 5 reserved.

- **The regression evidence that matters.** Because `None` is tried first, the
  shipped FFT64 fold resolves exactly as before: still spine column 4, still 84
  cells, **byte-identical layout** (`s0_ctl (4,0,east)`, `s5_out (4,11,east)`), and
  its 97 structural gates — the fit-limit suite, FFT16, and the repo-wide
  reachability gates — stay green. Any change to a placer must carry this kind of
  evidence: a placement that "still works" is not the same as one that is unchanged.

- **The second fix, once the corridor check was made HONEST: a height-capped
  fallback.** With `_corridors_ok` corrected to model the router (see the entry
  above), the reserved-lane fix alone was no longer enough — the die-0 chain still
  produced only tall folds, and now they were correctly rejected. The enumerator
  therefore falls back to searching a SHRUNK board: a height cap, starting at the
  shortest fold the chain can physically make (`h*W >= length`, and at least
  `2*(s+1)` rows for the spine) and growing. Capping the height is what makes the DFS
  spend its 400-candidate budget on WIDE, SHORT folds instead of rediscovering the
  same wall. Die 0 went from unplaceable to placing in **0.2 s**, in rows 0..7,
  leaving rows 8..11 free as a clean port-to-port corridor.

  **Order it so already-working sizes pay nothing:** the FULL-height batch is tried
  FIRST, so a size that already places never generates the fallback batches — no time
  cost, and a bit-identical layout (FFT64 held at `e27f020b27441656`). A fallback that
  reorders the search for inputs the old path already handled is a regression waiting
  to happen.

- **The general lesson.** When a generate-and-test placer fails, ask whether the
  GENERATOR can see the constraint the TEST enforces. If it cannot, the test is
  measuring a biased sample and "no solution exists" is an unsafe conclusion. Check
  it the cheap way first: strip the constraint, ask the generator whether ANY
  candidate exists, and compare. Here that took one call and turned a presumed
  geometric wall into an enumeration-order bug. And note the ordering of the two
  fixes: the port-blind enumerator was real, but it was only HALF the problem, and
  the half that was hiding the other half was the proxy corridor check.

## FFT64 chip-scale — two defect classes that BUILD CLEANLY and RUN WRONG: state pinned into instructions, and an unreachable dispatch entry 2026-08-24

The block placed, routed, built and ran all 84 cells (previous entry) and was still
wrong. Two independent faults, both of which pass every static check the repo had,
and both of which generalize well beyond this block.

- **INV-33's OVERLAP half: a cell that is EXACTLY 32/32 words silently pins its
  STATE on top of its own first instruction.** The word-count gate everyone writes
  is `max_addr + 1 + instr_count <= 32`, and a cell at exactly 32 passes it. But
  the resolver lays instructions DOWNWARD from address 30 to
  `base_addr = 31 - instr_count`, and it honours an explicitly-pinned
  `StateVar(register=N)` wherever it is told — including inside that range. The
  resolver's OWN guard only compares DATA against `base_addr`; it never checks
  state. So the cell assembles, the bitstream loads, the program runs ONCE, and
  the first `MOVE R{state}, R0` zeroes the instruction word the next trigger enters
  at. Symptom: the block emits exactly one sample and goes quiescent — which looks
  identical to a serialize-LOCK that never clears, and cost a whole dispatch chasing
  the lock. **Three cells were in this state** (`s0_mcalc` t@8/base 8,
  `s1_fetch_d` ptr@21/base 21, and `tab_d` at M=16 ad@21/base 21).
  **Gate it directly** (`test_fft64_fit_limit.py`): for every authored cell, assert
  no data address and no state register is `>= 31 - instr_count`. Cheap, static,
  and it caught a third instance the chip run had not yet reached.
  Freeing the word must NOT change arithmetic. Two moves did it here, both
  proven by running the reduced cell against the UNREDUCED one on real cells over
  every input: (a) delete genuinely dead words — a `BR.Z +0` pad (a conditional
  negate only ever needs to skip the negate itself, since the not-taken path
  already holds the non-negative value) and a `CMP` that re-derives a Z flag an
  earlier `SUB` already set (MOVE does not touch flags); (b) move a forwarded word
  to the **accumulator-delivery idiom** — it arrives in R0 and the cell's FIRST
  instruction re-emits it, freeing both the input register and the staging MOVE.
  (b) is nearly free whenever a cell merely passes a value through.

- **A dispatch ENTRY that nothing jumps is dead code, and no Python-side check can
  see it.** In the TwiddleMultiply idiom, path identity travels as WHICH ENTRY the
  next cell is jumped at. The octant fold's `sign` cell has `num` and `triv`
  entries, but `swap` was given ONE jump port wired unconditionally to `num`, so
  `triv` was unreachable and the two structurally trivial slots (k = 0, k = N/4)
  emitted numeric words instead of the sentinel encoding `steer` dispatches on.
  Two wrong twiddles per 32-slot cycle put the ENTIRE odd-bin half of every frame
  wrong while every even bin stayed right.
  **Three things hid it, each worth remembering:**
  1. The standalone fold-chain check drove `sign`'s entries BY HAND from the
     control word, so it exercised code the built chip could not reach. **A
     cell-level harness must read the dispatch decision OFF the cell** (here: the
     `triv` exit is the only path that writes no magnitudes) and assert it agrees
     with the control word — never re-decide it in the harness.
  2. An 80-sample chip run was clean and looked like proof. It is not: at N = 64
     the first valid output is 63, and frame slots 0..31 are the EVEN bins (the
     sum branch). **A run must reach output 95 before it has tested the twiddled
     half at all.** Size every streaming gate by what it REACHES, not by how many
     samples it sends.
  3. Every counter was correct on chip (`seq` p and `ctl` cnt in lockstep), so
     counter-skew models were a dead end. What found it was reading the `steer`
     cell's latched `(csav, dsav)` off the running chip trigger by trigger and
     comparing with the slot the stage should be using — the wrong slot appeared
     at exactly triggers 0 and 16, the two trivial slots, and nowhere else.
     **When arithmetic models cannot reproduce an on-chip divergence, stop
     modelling and read the intermediate state off the chip.**
  A single impulse is the sharpest stimulus for this class: it makes the ideal
  output a constant, so a phase error reads directly as a rotation
  (here `512 -> (510, -50)` = exactly `512 * W_64^1`).
## The 31-hop ceiling is LIFTED — relay emission; and the gru_classifier wall is NOT hop count 2026-08-24

Two linked results: an engine fix that landed and is proven on chip, and an
example that is still blocked — for a reason the fix does not touch.

### The fix: emit relay programming for >31-hop routes

A `WRITE`/`JUMP` carries a **5-bit HOP_CNT**, so one emission reaches at most 31
hops. Measured, not assumed: `@31` assembles and delivers; `@32` raises
`Distance must be 0-31` at assembly time. A word travels by being forwarded and,
when HOP_CNT hits 31, the arriving cell **executes it locally** instead
(`execute_locally` in the simulator trace) — that landing behaviour is the hook
the whole fix hangs on.

The router had ALREADY planned relay cells for over-budget routes and then failed
the net with *"relay programming is not yet emitted by the build"*. So this was
mostly a BUILD-side gap, exactly as suspected — worth checking before designing
anything, because the plan half already existed and was already tested
(`test_bus_relay.py`).

What was added: the word is addressed to LAND on an intermediate plain routing
cell, whose relay program flips to the route's continuation face and re-emits the
payload AND the trigger with a fresh budget:

```
MOVE [FACE], <exit_face>     ; point at the rest of the route
MOVE R0, R{in:burst}
WRITE @<next_seg>, <dest>    ; the PAYLOAD
JUMP  @<next_seg>, <entry>   ; the TRIGGER
HALT
```

That is the CrossoverBlock land→flip→re-emit primitive with one track — a relay
is a crossover whose purpose is a fresh hop count rather than a face demux. Reuse
it; do not write a second relay program.

Four things that had to be right:

1. **Emit the JUMP as well as the WRITE.** Forwarding only the payload gives a
   silent stream that never triggers the destination.
2. **Chain BACKWARD.** Program the last relay first, because each relay must
   address the *resolved entry* of the relay after it; the source is re-pointed
   at the first one last. The final relay reproduces the net's ORIGINAL
   dest/entry, read from the source's already-patched exit WRITE/JUMP — so the
   destination cannot tell a relayed delivery from a hop-legal one.
3. **One planner, four call sites.** `bus_router._plan_relays` is shared by the
   router, the build, the DRC's `hop_overflow`, and the controller's `add_route`
   guard. Three separate gates independently enforced "≤31 hops"; if they had
   been relaxed independently they would have disagreed about which routes are
   buildable. `relay_plan` DERIVES the cells from the routed project
   (build-from-design), so hand-placed `.kyt` files get relays too.
4. **Never relay onto a used cell** — a block cell, a USED chip-port cell, or an
   existing broker is a hard, NAMED rejection (INV-32 / port_transit).

**Proof.** A 52-hop egress — impossible before — routes, builds and runs
**bit-exact against a hop-legal short-route control**, which is the strongest
available statement: the relay is transparent to the data. A 96-hop route chains
THREE relays (segments 30/30/30/6) and still delivers, so relays compose and no
practical ceiling remains beyond array area. Cost is ~1 cell per 30 hops and is
reported (`ChipBuild.relay_cells` / `.relay_cost`) rather than silently spent.

**A mutation-design lesson worth keeping.** The first "wrong exit face" mutation
LEAKED — output stayed correct. It was not a relay bug: rotating the face
south→east sent the word into a free neighbouring cell that happened to forward
it back onto the corridor further along. Two things fixed the gate: mutate the
face constant **directly in the loaded chip memory** (so nothing else about the
build can differ), and choose the faces that genuinely leave the route (reverse /
perpendicular-away) instead of an arbitrary rotation. A mutation that a lucky
geometry can repair is a weak mutation — verify the mutant actually breaks the
path, don't just assume a changed word means a changed behaviour.

### The gru_classifier example is still blocked, and NOT by hops

The obvious hypothesis — lifting the ceiling unblocks the classifier — is
**false**, and it was worth measuring rather than assuming. After the fix: 2160
fresh layouts (18 legal GRU anchors × randomized small-block placement) plus a
sweep over four routing MODELS (`auto_orient`, the single-backbone bus/ring v2
router, orient+bus, cpsat). The result reproduces the prior attempt exactly —
best is always **exactly one net short**, and the reason is always
`no bus path from source to the broker tap`. Not one failing net on any layout
was a `hop_overflow`.

So the wall is **corridor congestion**, not hop budget. The diagnosis for whoever
picks it up: the blocks total only 65/120 cells, but `GRUCellBlock` is 51 of them
in a RIGID 7×8 fold that partitions the free area into pockets the 4-block RMS
arm cannot thread; the `join→GRU` tail alone routes at 72/120. The lever is the
GRU's FOLD (narrower / reflowable) or an RMS arm with fewer separately-placed
blocks — not the router. The three known-limit guards are deliberately left in
place and still hold; they fail the day the geometry gives.

## gru_classifier example — front end DERIVED and verified offline, whole-chain placement BLOCKED one net short 2026-08-24

The end-to-end 4-class modulation classifier (SSB / BPSK / 4-FSK / noise) on one
10x12 array. The feature front end is fully derived, measured, and bit-exact
against the trained model's own offline definition; the assembled chain does
**not** route as one chip. Reporting `needs_human` with the exact shortfall —
per §5b, an example that has not been observed producing the right output on a
placed + routed chip is NOT done, and no part of this entry claims otherwise.

- **THE RMS ARM DECOMPOSITION IS CORRECT AND ITS TOLERANCE IS DERIVED, NOT
  TUNED.** `ComplexToMagSquared -> MovingAverage(32, taps 1/32) -> Sqrt ->
  KeepOneInN(32)` reproduces `features.py`'s `sqrt(mean |x|^2)` over a
  non-overlapping 32-sample window. Every stage truncates downward, so the
  budget is: 2 LSB of power (magsq's two truncating products) + 32 LSB (the 32
  truncating MA taps; 1024 = 1/32 is exact) = **34 LSB of power deficit**, then
  `Sqrt`'s own measured `[-4, +1]` LSB. Propagating a power deficit through the
  root by `dy = dP / (2*sqrt(P))` makes the bound **input-level dependent**:
  `-(34 * 16384 / y) - 4 <= (chip - ideal) <= +1`, y = the RMS word. Measured
  over 1600 windows (4 classes x 5 peak levels): **0 violations, tightest case
  at 0.639 of the bound**. A FLAT tolerance would have been wrong — the same
  chain reads -13 LSB on a loud window and -218 on a quiet one, and the
  difference is the arithmetic, not noise.
- **ZCR is BIT-EXACT (0 / 400 windows) against its PINNED convention** — 32
  pairs *ending* at the window's samples (so the inter-window boundary pair is
  included) plus one implicit non-negative predecessor. Against plain
  `features.py` (31 strictly-interior pairs) it reads +1 crossing = +1024 Q15
  LSB on 6-51% of windows depending on class. Derived and gateable; not error.
- **THE Q15 POWER STAGE AND THE TRAINED DISTRIBUTION ARE IN GENUINE TENSION —
  and this is a MODEL property, not a chip bug.** `ComplexToMagSquared`
  saturates at full scale, so any `|z| >= 1` clips and biases that window's mean
  power DOWNWARD. But the model's own training channel sets a clip's *RMS*
  (`gain_range` 0.25..0.7) while saturation is driven by its *PEAK*, and the
  classes' crest factors differ sharply (measured, 12 clips each: 4-FSK 1.27,
  BPSK 1.71, noise 3.10, SSB 3.59 median). Over the shipped training set
  **`peak|z| > 1` for 100% of SSB and 79% of noise clips.** The float
  `features.py` never notices; a Q15 front end clips hard — measured error blows
  from the derived tens-of-LSB to **-1247 LSB** on such clips. Lesson: when a
  trained model is ported to Q15, check the PEAK distribution of its own
  training data against the fixed-point rails before trusting any feature
  tolerance — an offline reference that cannot saturate will not warn you.
  (Mitigation used here: pin per-segment gains at the low end of the trained
  range so `peak|z| < 0.95`; the stimulus asserts it rather than assuming it.)
- **Rescaling the input is NOT a free fix.** ZCR is scale-invariant but RMS is
  linear in input gain, so a global rescale moves a real feature off the grid
  the model was trained on. Peak-normalising 4-FSK clips to 0.9 made the
  *offline* reference vote BPSK — the chain was right and the stimulus was
  out of distribution. Always check a "fix" against the offline path first.
- **THE WALL: the chain does not route as one chip — always exactly ONE net
  short.** GRUCellBlock is 51 cells in a fixed 7x8 fold (56 of the array's 120
  cell sites) with BOTH its `f` input and its `oout` egress on the WEST edge.
  Measured: the GRU **alone** costs **64-82 cells including routing** depending
  on anchor (cheapest (3,1); (0,4)/(1,4)/(3,0) do not route at all). The
  join-tail — `KeepOneInN + ZCR + FeaturePairJoin + GRU` with both ingress nets
  and the egress — **does route, at 72 cells**, leaving 48 free. But those 48
  are split into two pockets by the ingress corridors, and the RMS arm (power 1
  + boxcar 2x4 + root 2x2 = 11 block cells) must simultaneously reach the port
  (top-left, walled by the ingress `rr` corridor) and `decim`.
  **~2500 distinct layouts** were tried across three independent strategies —
  exhaustive hand-anchor sweeps (`auto_route_all`, deterministic), randomized
  hand placement over all routable GRU anchors, and `auto_pnr` with the GRU
  pinned (stochastic, many repeats) — plus `auto_orient=True`. The best result
  was **never better than one failing net**, and WHICH net fails rotates between
  `root_decim`, `rms_join`, `zcr_join`, `gru_out` and the ingress trio as blocks
  move: the signature of a saturated array, not one bad anchor. Two recurring,
  citable causes: `bus route is N hops (>31)` on the GRU egress (34-36 hops —
  the 5-bit hop field, and relay programming is not emitted by the build), and
  `no free broker cell abutting the target input` when a 1-cell block is packed
  against a wall or the port corner.
- **What a human should look at.** Either (a) the GRU's west-edge-only I/O — an
  egress relay reachable from the EAST would remove the wrap that costs the
  34-hop `gru_out` failures; or (b) a smaller RMS arm (the 7-cell
  `MovingAverage(32)` is the bulk of the front end); or (c) relay programming
  for >31-hop bus routes, which the router already plans but the build does not
  emit. The offline chain, the goldens, the derived tolerances, and the
  stimulus are all in place and re-usable the moment the geometry gives.
`LargeFFTBlock` to N=32. Bit-exact on a real built chip, 75 gates green. It is
enters at. **The cell is EXACTLY full at 32/32 words, and the count gate passes.** This is byte-for-byte
state at 21 — 30/32 words at P=16 with the entry instruction two words clear of the state, where before they collided. **This also repairs one of
at N=16 over the SAME 40 seeds shows **the shipped FFT16 reaches the same
clamp on 3 of 40 — the identical rate** — and FFT16's OWN gated seed (101)
measures ZERO clamps, so its published 78.8 dB figure for that class simply
used a seed that did not clamp. So this is a property of the pinned numerics at both sizes, not an N=32
The spine solve is a backtracking search costing ~28 s, so it is now memoized
### A block that CANNOT be constructed must leave the CATALOG, not just fail

Found while regression-testing this build, and PRE-EXISTING on main (verified
by re-running at the parent commit): six tests across `test_data_words.py` and
`test_portmap.py` were red, all with the same cause — `FFT128Block` is
catalogued, and every "build/portmap each catalog block" sweep instantiates it,
and its constructor correctly raises `LargeFFTGeometryError` (14 spine rows
against a 12-row panel). A LOUD constructor failure is the right design; being
in the catalog while having no in-array implementation is not. It is now in
`catalog._EXCLUDED_BLOCKS` — which is exactly what that list is for — with the
2-die-split condition for removing it recorded next to the entry, and its
`NEEDS_BESPOKE` saturation reason kept so that re-catalogueing it can never
silently skip a gate. Six tests green, none of them weakened.

The general form: an "every catalog block" sweep is a good gate, and the way to
keep it honest is to make the catalog mean "can be instantiated on this array",
not to add exceptions inside the sweep.

## FFT32Block — the family's third size: no fold needed, and the two INVISIBLE defects the P=16 boundary exposes 2026-08-24

A 60-cell, 5-stage streaming R2SDF FFT (delays 16/8/4/2/1, latency 31, output
in BIT-REVERSED bin order, scale FFT/32), built by parameterising the landed
`LargeFFTBlock` to N=32. Bit-exact on a real built chip, 74 gates green. It is
the EASIEST size in the family on arithmetic and the one that found the most
bugs — because N=32 is the first size that hits the P=16 direct-table
boundary, and because its layout comes from a SEARCH rather than by hand.

**THE COST ANSWER: no octant fold, and it was not close.** Every N=32 stage's
twiddle period is at most 16 = `DIRECT_TABLE_MAX`, so all three twiddle stages
use the shipped DIRECT-table chain and the 9-cell octant fold that N=64 needs
is never reached (gated, not assumed: `uses_fold(s)` is False for every stage
and no `seq`/`mcalc`/`tab_*`/`swap`/`sign` cell exists in the block). Measured
budget: 5 stages x (7-cell spine + 5 twiddle or 0) + delay cells, with two
PARITY PADS, = 16+14+14+8+8 = **60 cells**. That is 24 fewer than N=64 for
half the transform — the fold, not the stage count, is what makes N=64
expensive.

**PER-STAGE BANDS vs THE SPINE: the spine, and the reason is HEIGHT not area.**
60 cells would have fitted the ordinary 8x8 = 64-cell cap comfortably, and the
FFT16 band scheme was the first thing tried. It cannot work: `2 * n_stages` =
**10 rows** of ctl/out spine against an 8-row ordinary cap. So FFT32 is
CHIP_SCALE — but a modest one (9 wide x 10 tall, leaving column 9 and rows
10-11 free for the port corridors), not a die-filler. The honest statement is
that this block is chip-scale on the SPINE HEIGHT alone, and the suite asserts
exactly that justification (`test_the_spine_is_why_this_size_is_chip_scale`)
so it cannot rot into folklore.

### DEFECT 1 — the P=16 direct table cell OVERWRITES ITS OWN PROGRAM (INV-33)

The N=32 stage 0 is the first stage in the family with a 16-entry direct
twiddle table. The shipped FFT16 `fetch_d` also CROSS-FORWARDS the `c` word
it receives from `fetch_c`, which costs 2 instructions. At P=16 that makes the
cell 1 input + 19 data + 10 instructions:

    instruction base = 31 - 10 = 21
    the single remaining gap register = 21
    -> the resolved `ptr` state IS the entry instruction

The cell's first `MOVE R{state:ptr}, R0` destroys the word the next trigger
enters at. **The 32-word COUNT gate passes at 31/32.** This is byte-for-byte
the defect the FFT64 entry below root-caused, and it is why that entry's
durable lesson matters: *"every cell fits the budget" is not the same check as
"no cell's state overlaps its instructions"* — and a cell that is EXACTLY full
is the danger case.

The fix is to remove the cross-forward: each table cell now writes its word
DIRECTLY into `steer`'s own `c` / `d` input register (a 2-hop and a 1-hop
write, traced along the chain's resting faces exactly like the sum legs'
multi-hop write into `gather`). Both cells drop to 8 instructions, base 23,
state at 21 — 23/32 words at P=16, comfortable. **This also repairs one of
FFT64's two overlapping cells for free** (`s1_fetch_d`); its `s0_mcalc` fold
cell still overlaps, so FFT64 remains needs_human, but it is now one defect
rather than two.

New GENERAL lesson, on top of the FFT64 one: a shared cell builder that is
safe at every size shipped SO FAR can be fatal at the next size, because the
budget interacts with a PARAMETER (here the table length). When
parameterising a proven builder, re-measure the overlap at the LARGEST
parameter value, not the one already in service. The gate
(`test_no_state_overlaps_instructions`) is cheap and is now shown to FAIL on
the pre-fix shape (`test_state_instruction_overlap_gate_has_teeth`
reconstructs it), which is the only way to know it has teeth.

### DEFECT 2 — the ROUTE-TIME FACE RULE must be a placement CONSTRAINT, not an audit

With the overlap fixed, the block built, routed, and ran **exactly 4 samples**
on a real chip, then went quiescent. Every static gate was green: 60/60 cells
placed, ctl/out/next-ctl stacked for every stage, all consecutive chain pairs
adjacent, and **0 hop mismatches across all 241 forward internal edges**.

The `hopcheck` audit passed and the block still did not run, which is the part
worth remembering. The audit only traces FORWARD edges from source to
destination; it does not ask whether a cell's RESTING face points where the
chain needs it. The searched fold had placed every stage's `diffq`
edge-adjacent to its own `d0` (the delay push). `diffq`'s LAST-listed internal
connection is `v_f -> d0`, so the router set `diffq`'s route-time face toward
`d0` instead of toward its chain successor — and any trace passing THROUGH
`diffq` then diverges, silently, via the Manhattan fallback.

This is precisely the hazard the shipped FFT16 avoids BY HAND (its
`_stage_cells` comment: *"a diff leg sitting directly beside its delay-push
target mis-faced the whole ring and silently shipped Manhattan hops"*). FFT16
gets it right because a human placed those cells. **A searched fold has no
such guarantee, and `fft_large`'s docstring already referenced a
`_face_rule_ok` that was never implemented** — a dangling reference that was
exactly the missing constraint.

The fix implements it and calls it INSIDE `_solve_spine`, so candidate chains
that would mis-face are rejected during the search rather than audited
afterwards. Cost: the same 60 cells and the same spine column; the solver just
picks a different walk. The four structural audits then read 0/0/0 and
0 mismatches / 241 edges, and the chip runs the whole stream bit-exact.

Durable form: **a geometric rule that a hand-placed block satisfies by
craftsmanship becomes a CONSTRAINT the moment the layout is searched.** Every
such rule in a planner needs to be a predicate the search consults, not a test
that runs after. And an audit that passes while the chip stalls is telling you
the audit checks a different thing than the one that is broken — here,
forward-edge hops vs resting faces.

### SNR: measured, and where N=32 sits in the family

Floors are the measured MINIMUM over 40 seeds per class (model-side; the chip
is bit-exact to the model), rounded down — derived, never tuned:

    class        gate seed   min/40   mean/40   pinned floor
    sine_fs         83.44     81.33     86.95        81
    noise_m6        74.47     73.02*    72.48*       72   (*unclamped seeds)
    noise_m26       54.59     53.68     54.69        53
    two_tone        73.16     72.59     73.42        72
    impulse         66.77     63.44     66.03        63

The weakest class (noise at -26 dBFS, floor 53 dB) sits BETWEEN the shipped
N=16 floor (58 dB) and the N=64 design floor (51 dB) — one more scaled stage,
monotonically more accumulated quantization noise.

**DISCLOSED, and it corrects a claim about the shipped block.** 3 of 40
noise-at--6-dBFS seeds reach the TWIDDLE-MULTIPLY saturating combine in an
intermediate stage and their pooled SNR collapses to ~32 dB. That is CORRECT
pinned behaviour (a saturating rail), and instrumenting the same drive level
at N=16 shows **the shipped FFT16 reaches the same clamp on 2 of 20 seeds** —
its published 78.8 dB figure for that class simply used a seed that did not
clamp. So this is a property of the pinned numerics at both sizes, not an N=32
regression. The class is gated on a seed measured NOT to clamp, and both
halves of the statement are asserted
(`test_noise_m6_clamp_reachability_is_disclosed`) so the fact cannot be lost.

Related measurement worth recording: **which clamp is reachable changes with
N.** At N=16 the one reachable clamp was the butterfly's RHE diff-leg tie; at
N=32 that tie is unreachable on every gated class (asserted as an explicit
negative), and the reachable clamp is the twiddle combine, fired 15-21 times
per run by the both-rails-full class. A clamp gate copied from the previous
size would have certified nothing — check which path your stimulus actually
reaches before writing the gate.

### Two smaller things

- **A 1-LSB twiddle corruption is usually INVISIBLE.** The twiddle multiply is
  four FLOOR MULQs, so a 1-LSB coefficient change is frequently absorbed by
  the truncation. Measured on the gate stimulus: only slots {1,2,6,9} of
  stage 0, {2,3} of stage 1 and {1,3} of stage 2 are detectable. A mutation
  test that picks "the first non-trivial slot" therefore fails as a
  NO-TEETH assertion, not as a block defect. The gate now measures which
  slots are detectable, gates ALL of them, and asserts every twiddle stage
  has at least one — which is a stronger test than the arbitrary single slot
  it replaced.
- **Frame-boundary carry means the opposite of what it sounds like.** For a
  windowed transform, frame B's SETTLED output must be INDEPENDENT of the
  frame that preceded it; a pipeline leaking state across the boundary would
  break that. The teeth live in the predecessor's own window: crafted
  adjacent frames differ in EXACTLY the 32 outputs of the A window and
  nowhere else. Asserting "B differs after a different A" is the wrong test
  and will fail on a correct block.

The spine solve is a backtracking search costing ~65 s, so it is now memoized
per N (`_PLAN_CACHE`); every instance of a size gets the identical layout,
which the suite asserts (`test_layout_is_deterministic`).

## gru_classifier example — front end DERIVED and verified offline, whole-chain placement BLOCKED one net short 2026-08-24

The end-to-end 4-class modulation classifier (SSB / BPSK / 4-FSK / noise) on one
10x12 array. The feature front end is fully derived, measured, and bit-exact
against the trained model's own offline definition; the assembled chain does
**not** route as one chip. Reporting `needs_human` with the exact shortfall —
per §5b, an example that has not been observed producing the right output on a
placed + routed chip is NOT done, and no part of this entry claims otherwise.

- **THE RMS ARM DECOMPOSITION IS CORRECT AND ITS TOLERANCE IS DERIVED, NOT
  TUNED.** `ComplexToMagSquared -> MovingAverage(32, taps 1/32) -> Sqrt ->
  KeepOneInN(32)` reproduces `features.py`'s `sqrt(mean |x|^2)` over a
  non-overlapping 32-sample window. Every stage truncates downward, so the
  budget is: 2 LSB of power (magsq's two truncating products) + 32 LSB (the 32
  truncating MA taps; 1024 = 1/32 is exact) = **34 LSB of power deficit**, then
  `Sqrt`'s own measured `[-4, +1]` LSB. Propagating a power deficit through the
  root by `dy = dP / (2*sqrt(P))` makes the bound **input-level dependent**:
  `-(34 * 16384 / y) - 4 <= (chip - ideal) <= +1`, y = the RMS word. Measured
  over 1600 windows (4 classes x 5 peak levels): **0 violations, tightest case
  at 0.639 of the bound**. A FLAT tolerance would have been wrong — the same
  chain reads -13 LSB on a loud window and -218 on a quiet one, and the
  difference is the arithmetic, not noise.
- **ZCR is BIT-EXACT (0 / 400 windows) against its PINNED convention** — 32
  pairs *ending* at the window's samples (so the inter-window boundary pair is
  included) plus one implicit non-negative predecessor. Against plain
  `features.py` (31 strictly-interior pairs) it reads +1 crossing = +1024 Q15
  LSB on 6-51% of windows depending on class. Derived and gateable; not error.
- **THE Q15 POWER STAGE AND THE TRAINED DISTRIBUTION ARE IN GENUINE TENSION —
  and this is a MODEL property, not a chip bug.** `ComplexToMagSquared`
  saturates at full scale, so any `|z| >= 1` clips and biases that window's mean
  power DOWNWARD. But the model's own training channel sets a clip's *RMS*
  (`gain_range` 0.25..0.7) while saturation is driven by its *PEAK*, and the
  classes' crest factors differ sharply (measured, 12 clips each: 4-FSK 1.27,
  BPSK 1.71, noise 3.10, SSB 3.59 median). Over the shipped training set
  **`peak|z| > 1` for 100% of SSB and 79% of noise clips.** The float
  `features.py` never notices; a Q15 front end clips hard — measured error blows
  from the derived tens-of-LSB to **-1247 LSB** on such clips. Lesson: when a
  trained model is ported to Q15, check the PEAK distribution of its own
  training data against the fixed-point rails before trusting any feature
  tolerance — an offline reference that cannot saturate will not warn you.
  (Mitigation used here: pin per-segment gains at the low end of the trained
  range so `peak|z| < 0.95`; the stimulus asserts it rather than assuming it.)
- **Rescaling the input is NOT a free fix.** ZCR is scale-invariant but RMS is
  linear in input gain, so a global rescale moves a real feature off the grid
  the model was trained on. Peak-normalising 4-FSK clips to 0.9 made the
  *offline* reference vote BPSK — the chain was right and the stimulus was
  out of distribution. Always check a "fix" against the offline path first.
- **THE WALL: the chain does not route as one chip — always exactly ONE net
  short.** GRUCellBlock is 51 cells in a fixed 7x8 fold (56 of the array's 120
  cell sites) with BOTH its `f` input and its `oout` egress on the WEST edge.
  Measured: the GRU **alone** costs **64-82 cells including routing** depending
  on anchor (cheapest (3,1); (0,4)/(1,4)/(3,0) do not route at all). The
  join-tail — `KeepOneInN + ZCR + FeaturePairJoin + GRU` with both ingress nets
  and the egress — **does route, at 72 cells**, leaving 48 free. But those 48
  are split into two pockets by the ingress corridors, and the RMS arm (power 1
  + boxcar 2x4 + root 2x2 = 11 block cells) must simultaneously reach the port
  (top-left, walled by the ingress `rr` corridor) and `decim`.
  **~2500 distinct layouts** were tried across three independent strategies —
  exhaustive hand-anchor sweeps (`auto_route_all`, deterministic), randomized
  hand placement over all routable GRU anchors, and `auto_pnr` with the GRU
  pinned (stochastic, many repeats) — plus `auto_orient=True`. The best result
  was **never better than one failing net**, and WHICH net fails rotates between
  `root_decim`, `rms_join`, `zcr_join`, `gru_out` and the ingress trio as blocks
  move: the signature of a saturated array, not one bad anchor. Two recurring,
  citable causes: `bus route is N hops (>31)` on the GRU egress (34-36 hops —
  the 5-bit hop field, and relay programming is not emitted by the build), and
  `no free broker cell abutting the target input` when a 1-cell block is packed
  against a wall or the port corner.
- **What a human should look at.** Either (a) the GRU's west-edge-only I/O — an
  egress relay reachable from the EAST would remove the wrap that costs the
  34-hop `gru_out` failures; or (b) a smaller RMS arm (the 7-cell
  `MovingAverage(32)` is the bulk of the front end); or (c) relay programming
  for >31-hop bus routes, which the router already plans but the build does not
  emit. The offline chain, the goldens, the derived tolerances, and the
  stimulus are all in place and re-usable the moment the geometry gives.

## gru_classifier example — front end DERIVED and verified offline, whole-chain placement BLOCKED one net short 2026-08-24

The end-to-end 4-class modulation classifier (SSB / BPSK / 4-FSK / noise) on one
10x12 array. The feature front end is fully derived, measured, and bit-exact
against the trained model's own offline definition; the assembled chain does
**not** route as one chip. Reporting `needs_human` with the exact shortfall —
per §5b, an example that has not been observed producing the right output on a
placed + routed chip is NOT done, and no part of this entry claims otherwise.

- **THE RMS ARM DECOMPOSITION IS CORRECT AND ITS TOLERANCE IS DERIVED, NOT
  TUNED.** `ComplexToMagSquared -> MovingAverage(32, taps 1/32) -> Sqrt ->
  KeepOneInN(32)` reproduces `features.py`'s `sqrt(mean |x|^2)` over a
  non-overlapping 32-sample window. Every stage truncates downward, so the
  budget is: 2 LSB of power (magsq's two truncating products) + 32 LSB (the 32
  truncating MA taps; 1024 = 1/32 is exact) = **34 LSB of power deficit**, then
  `Sqrt`'s own measured `[-4, +1]` LSB. Propagating a power deficit through the
  root by `dy = dP / (2*sqrt(P))` makes the bound **input-level dependent**:
  `-(34 * 16384 / y) - 4 <= (chip - ideal) <= +1`, y = the RMS word. Measured
  over 1600 windows (4 classes x 5 peak levels): **0 violations, tightest case
  at 0.639 of the bound**. A FLAT tolerance would have been wrong — the same
  chain reads -13 LSB on a loud window and -218 on a quiet one, and the
  difference is the arithmetic, not noise.
- **ZCR is BIT-EXACT (0 / 400 windows) against its PINNED convention** — 32
  pairs *ending* at the window's samples (so the inter-window boundary pair is
  included) plus one implicit non-negative predecessor. Against plain
  `features.py` (31 strictly-interior pairs) it reads +1 crossing = +1024 Q15
  LSB on 6-51% of windows depending on class. Derived and gateable; not error.
- **THE Q15 POWER STAGE AND THE TRAINED DISTRIBUTION ARE IN GENUINE TENSION —
  and this is a MODEL property, not a chip bug.** `ComplexToMagSquared`
  saturates at full scale, so any `|z| >= 1` clips and biases that window's mean
  power DOWNWARD. But the model's own training channel sets a clip's *RMS*
  (`gain_range` 0.25..0.7) while saturation is driven by its *PEAK*, and the
  classes' crest factors differ sharply (measured, 12 clips each: 4-FSK 1.27,
  BPSK 1.71, noise 3.10, SSB 3.59 median). Over the shipped training set
  **`peak|z| > 1` for 100% of SSB and 79% of noise clips.** The float
  `features.py` never notices; a Q15 front end clips hard — measured error blows
  from the derived tens-of-LSB to **-1247 LSB** on such clips. Lesson: when a
  trained model is ported to Q15, check the PEAK distribution of its own
  training data against the fixed-point rails before trusting any feature
  tolerance — an offline reference that cannot saturate will not warn you.
  (Mitigation used here: pin per-segment gains at the low end of the trained
  range so `peak|z| < 0.95`; the stimulus asserts it rather than assuming it.)
- **Rescaling the input is NOT a free fix.** ZCR is scale-invariant but RMS is
  linear in input gain, so a global rescale moves a real feature off the grid
  the model was trained on. Peak-normalising 4-FSK clips to 0.9 made the
  *offline* reference vote BPSK — the chain was right and the stimulus was
  out of distribution. Always check a "fix" against the offline path first.
- **THE WALL: the chain does not route as one chip — always exactly ONE net
  short.** GRUCellBlock is 51 cells in a fixed 7x8 fold (56 of the array's 120
  cell sites) with BOTH its `f` input and its `oout` egress on the WEST edge.
  Measured: the GRU **alone** costs **64-82 cells including routing** depending
  on anchor (cheapest (3,1); (0,4)/(1,4)/(3,0) do not route at all). The
  join-tail — `KeepOneInN + ZCR + FeaturePairJoin + GRU` with both ingress nets
  and the egress — **does route, at 72 cells**, leaving 48 free. But those 48
  are split into two pockets by the ingress corridors, and the RMS arm (power 1
  + boxcar 2x4 + root 2x2 = 11 block cells) must simultaneously reach the port
  (top-left, walled by the ingress `rr` corridor) and `decim`.
  **~2500 distinct layouts** were tried across three independent strategies —
  exhaustive hand-anchor sweeps (`auto_route_all`, deterministic), randomized
  hand placement over all routable GRU anchors, and `auto_pnr` with the GRU
  pinned (stochastic, many repeats) — plus `auto_orient=True`. The best result
  was **never better than one failing net**, and WHICH net fails rotates between
  `root_decim`, `rms_join`, `zcr_join`, `gru_out` and the ingress trio as blocks
  move: the signature of a saturated array, not one bad anchor. Two recurring,
  citable causes: `bus route is N hops (>31)` on the GRU egress (34-36 hops —
  the 5-bit hop field, and relay programming is not emitted by the build), and
  `no free broker cell abutting the target input` when a 1-cell block is packed
  against a wall or the port corner.
- **What a human should look at.** Either (a) the GRU's west-edge-only I/O — an
  egress relay reachable from the EAST would remove the wrap that costs the
  34-hop `gru_out` failures; or (b) a smaller RMS arm (the 7-cell
  `MovingAverage(32)` is the bulk of the front end); or (c) relay programming
  for >31-hop bus routes, which the router already plans but the build does not
  emit. The offline chain, the goldens, the derived tolerances, and the
  stimulus are all in place and re-usable the moment the geometry gives.

## FFT64 chip-scale — the STAGE-BAND wall was NOT real; the VERTICAL CTL/OUT SPINE places and flows, one dynamic fault left 2026-08-24

The previous entry (below) concluded FFT64 "does not fit" on a band cap and a row
budget. **Both walls were artefacts of the layout convention, not the fabric.** 84
cells in a 120-cell array is 70% utilisation; the cells always fit, the assumed
rectangles did not. What follows is the corrected geometry, the two constraints
that ARE real, and an honest statement of what still does not work.

- **THE PARITY THEOREM (new, general — promote-worthy).** A chain of `L` cells can
  land its LAST cell edge-adjacent to its FIRST only when `L` is EVEN. Chessboard-
  colour the array: every step to an edge-adjacent cell flips the colour, so cell
  `L-1` has the start's colour XOR `(L-1) % 2`, while adjacency demands the
  opposite colour. Proven by the argument AND by exhaustive search over all
  self-avoiding walks for L = 2..12. This is WHY the shipped FFT16 (stages
  14/14/8/8, all even) folded tidily with no effort, and why the old planner's
  `L = 2W - 2*(a mod W)` condition looked like a band rule: it is the parity
  theorem seen through one particular rectangle. **Any block whose last cell must
  abut its first inherits this.** The repair is cheap when the chain contains a
  delay line: spread it over ONE extra cell (`_delay_segments(..., extra_cells=1)`)
  — identical total delay, even chain. N=64 pads 3 of 6 stages, 81 -> 84 cells.
- **THE REAL GEOMETRY: a stage's `out` needs THREE @1 neighbours, so the whole
  ctl/out sequence is ONE COLUMN.** FFT16 gets all three from one arrangement and
  it generalises exactly: its own `ctl` directly ABOVE (write-back + lock-clear on
  the in-program `face_fb` = NORTH); the NEXT stage's `ctl` directly BELOW (forward
  packet on `face_tap` = SOUTH, which is ALSO the resting face, hence what the
  router traces); and for the last stage a free neighbour the egress corridor
  starts from. So the spine is `2 * n_stages` rows in one column.
- **WHY that is forced, and the trap that hides it: a bad internal hop does NOT
  error.** `router._get_routing_distance` traces resting faces and **silently falls
  back to MANHATTAN distance** when the trace fails. A non-traceable inter-stage
  handoff therefore ships a WRONG hop; the packet lands in the wrong cell and the
  stage spins on its own ring. Measured: with the stages shelf-packed side by side
  (out facing its own ctl, next ctl elsewhere) stage 0 alone consumed 1e6 simulator
  events and stage 1 never ran. A `hopcheck` audit — trace every forward internal
  edge and compare with its chain distance — turns this into a static gate: FFT16
  scores 0 mismatches / 188 edges, and the fold is only trustworthy at 0/349.
- **The ROW BUDGET was over-charged by one.** The old note used 11 usable rows
  because "a 10-wide block cannot use row 0 without its ctl column landing on the
  port cell". True for a 10-wide fold — but the spine only needs its OWN column to
  avoid columns 0 and 9, and then **row 0 IS usable**. N=64's 6 stages need 12 rows
  and the array is 12 tall: it fits exactly. N=128's 7 stages need 14 and is ruled
  out on the spine height (not on area) — the 2-die split is its topology.
- **Packing tightly is a routing bug.** The first spine solution filled all 12 rows
  and failed with "no free corridor between the ports" / "endpoint cell is off the
  array grid". The placement must itself check that free (non-block) cells still
  connect the input landing to `x16_in` and the exit to `x16_out` — the same reason
  the shipped FFT16 is 7 wide on a 10-wide array. Added as `_corridors_ok`, a
  4-connectivity check over the free cells, run as the last step of the solve.
- **The block EXIT must not rest toward its own ctl.** The resting face is what the
  build rewrites to the routed egress (`output_face_addr`) and what the router
  traces; a last `out` resting back into its ctl re-enters the stage instead of
  leaving the block. FFT16's last `out` rests SOUTH, away from its ctl, into the
  cell the egress corridor starts from.
- **A general Hamiltonian-walk search is a WORSE fold than a boustrophedon.** Given
  freedom, the search returns face-rule-clean walks that zig-zag between two
  columns — and those pack the delay line, `out` and `ctl` into a maze of touching
  cells. Prefer wide-and-short boxes and the straightest walk; "clean by the stated
  rule" is not the same as "behaves like the shipped block".

**WHAT IS VERIFIED** (74 gates green: 37 arithmetic + FFT16's 37 unregressed):
the octant fold bit-exact at both N; every cell inside 32 words; and, on the REAL
built chip at N=64, that the design places, both nets route with real corridors,
the build succeeds, and driving it executes **all 84 cells** with per-stage activity
decreasing monotonically s0->s5. Structural audits are clean: every consecutive
pair adjacent, ctl/out/next-ctl stacked for every stage, ZERO route-time-face
violations, **0 hop mismatches across all 349 forward internal edges**.

**THE REMAINING BUG, ROOT-CAUSED: two cells OVERWRITE THEIR OWN PROGRAM — an
INV-33 state/instruction OVERLAP, and it is an arithmetic bug, not a layout one.**

Symptom: only the FIRST sample's word egresses; sample 1 runs stage 0 as far as
`s0_mcalc` and everything downstream never executes; by sample 2 the input
corridor is back-pressured and the chip is quiescent.

Found by dumping the cell's memory BEFORE and AFTER each driven sample: during
sample 0, `s0_mcalc`'s address **8 changes from 0x9823 (its FIRST INSTRUCTION) to
0x0000**. The cell's entry address is 8 AND its `StateVar("t")` resolves to
register 8 — so the first `MOVE R{state:t}, R0` destroys the instruction the next
trigger will enter at, and the second sample enters a HALT. The trace says this
outright once you know to look: `s0_mcalc exec_tick pc=8 word=0x0000 result=halt`,
while the *loaded* image at address 8 is a real instruction.

INV-33 predicts it exactly: state auto-lands in
`range(max_data_address + 1, 31 - instr_count)`, and `mcalc` is 2 inputs + 5 data
+ 1 state + 23 instructions = 31 words with data ending at 7 — so state lands at
8 and the instructions ALSO start at 8. A second cell has the identical defect:
`s1_fetch_d` (1 input + 19 data + 1 state + 10 instructions), entry 21, `ptr` at
21. An audit over every cell of both sizes finds exactly these two.

**Two durable lessons:**
- **"Every cell fits the 32-word budget" is NOT the same check as "no cell's state
  overlaps its instructions."** The prior dispatch gated the word COUNT and passed;
  a cell can total 31 words and still allocate state on top of its own program.
  The correct gate compares `min(entry addresses)` against every resolved state
  register — added to the suite. A cell that is EXACTLY full is the danger case.
- **A self-overwriting cell looks exactly like a fabric/handshake problem.** It
  presents as "runs once then the pipeline goes dead", which is the classic
  serialize-LOCK signature, and it survived every static audit (placement, faces,
  hops, lock-clear patching — all measured correct and identical to FFT16). Only a
  BEFORE/AFTER memory diff of the stalling cell distinguishes them. When a block
  runs exactly one sample, diff its cell memory before blaming the lock.

**FFT64Block is therefore NOT done**; it must not be described as working, and the
module docstring says so. The fix is to re-fit the two cells (each is at 31/32
words with every data word genuinely used, so it needs an instruction or a data
word removed, not a re-pin) and then re-run the on-chip gates. FFT128Block
correctly raises with the spine-height shortfall.
## gru_classifier example — front end DERIVED and verified offline, whole-chain placement BLOCKED one net short 2026-08-24

The end-to-end 4-class modulation classifier (SSB / BPSK / 4-FSK / noise) on one
10x12 array. The feature front end is fully derived, measured, and bit-exact
against the trained model's own offline definition; the assembled chain does
**not** route as one chip. Reporting `needs_human` with the exact shortfall —
per §5b, an example that has not been observed producing the right output on a
placed + routed chip is NOT done, and no part of this entry claims otherwise.

- **THE RMS ARM DECOMPOSITION IS CORRECT AND ITS TOLERANCE IS DERIVED, NOT
  TUNED.** `ComplexToMagSquared -> MovingAverage(32, taps 1/32) -> Sqrt ->
  KeepOneInN(32)` reproduces `features.py`'s `sqrt(mean |x|^2)` over a
  non-overlapping 32-sample window. Every stage truncates downward, so the
  budget is: 2 LSB of power (magsq's two truncating products) + 32 LSB (the 32
  truncating MA taps; 1024 = 1/32 is exact) = **34 LSB of power deficit**, then
  `Sqrt`'s own measured `[-4, +1]` LSB. Propagating a power deficit through the
  root by `dy = dP / (2*sqrt(P))` makes the bound **input-level dependent**:
  `-(34 * 16384 / y) - 4 <= (chip - ideal) <= +1`, y = the RMS word. Measured
  over 1600 windows (4 classes x 5 peak levels): **0 violations, tightest case
  at 0.639 of the bound**. A FLAT tolerance would have been wrong — the same
  chain reads -13 LSB on a loud window and -218 on a quiet one, and the
  difference is the arithmetic, not noise.
- **ZCR is BIT-EXACT (0 / 400 windows) against its PINNED convention** — 32
  pairs *ending* at the window's samples (so the inter-window boundary pair is
  included) plus one implicit non-negative predecessor. Against plain
  `features.py` (31 strictly-interior pairs) it reads +1 crossing = +1024 Q15
  LSB on 6-51% of windows depending on class. Derived and gateable; not error.
- **THE Q15 POWER STAGE AND THE TRAINED DISTRIBUTION ARE IN GENUINE TENSION —
  and this is a MODEL property, not a chip bug.** `ComplexToMagSquared`
  saturates at full scale, so any `|z| >= 1` clips and biases that window's mean
  power DOWNWARD. But the model's own training channel sets a clip's *RMS*
  (`gain_range` 0.25..0.7) while saturation is driven by its *PEAK*, and the
  classes' crest factors differ sharply (measured, 12 clips each: 4-FSK 1.27,
  BPSK 1.71, noise 3.10, SSB 3.59 median). Over the shipped training set
  **`peak|z| > 1` for 100% of SSB and 79% of noise clips.** The float
  `features.py` never notices; a Q15 front end clips hard — measured error blows
  from the derived tens-of-LSB to **-1247 LSB** on such clips. Lesson: when a
  trained model is ported to Q15, check the PEAK distribution of its own
  training data against the fixed-point rails before trusting any feature
  tolerance — an offline reference that cannot saturate will not warn you.
  (Mitigation used here: pin per-segment gains at the low end of the trained
  range so `peak|z| < 0.95`; the stimulus asserts it rather than assuming it.)
- **Rescaling the input is NOT a free fix.** ZCR is scale-invariant but RMS is
  linear in input gain, so a global rescale moves a real feature off the grid
  the model was trained on. Peak-normalising 4-FSK clips to 0.9 made the
  *offline* reference vote BPSK — the chain was right and the stimulus was
  out of distribution. Always check a "fix" against the offline path first.
- **THE WALL: the chain does not route as one chip — always exactly ONE net
  short.** GRUCellBlock is 51 cells in a fixed 7x8 fold (56 of the array's 120
  cell sites) with BOTH its `f` input and its `oout` egress on the WEST edge.
  Measured: the GRU **alone** costs **64-82 cells including routing** depending
  on anchor (cheapest (3,1); (0,4)/(1,4)/(3,0) do not route at all). The
  join-tail — `KeepOneInN + ZCR + FeaturePairJoin + GRU` with both ingress nets
  and the egress — **does route, at 72 cells**, leaving 48 free. But those 48
  are split into two pockets by the ingress corridors, and the RMS arm (power 1
  + boxcar 2x4 + root 2x2 = 11 block cells) must simultaneously reach the port
  (top-left, walled by the ingress `rr` corridor) and `decim`.
  **~2500 distinct layouts** were tried across three independent strategies —
  exhaustive hand-anchor sweeps (`auto_route_all`, deterministic), randomized
  hand placement over all routable GRU anchors, and `auto_pnr` with the GRU
  pinned (stochastic, many repeats) — plus `auto_orient=True`. The best result
  was **never better than one failing net**, and WHICH net fails rotates between
  `root_decim`, `rms_join`, `zcr_join`, `gru_out` and the ingress trio as blocks
  move: the signature of a saturated array, not one bad anchor. Two recurring,
  citable causes: `bus route is N hops (>31)` on the GRU egress (34-36 hops —
  the 5-bit hop field, and relay programming is not emitted by the build), and
  `no free broker cell abutting the target input` when a 1-cell block is packed
  against a wall or the port corner.
- **What a human should look at.** Either (a) the GRU's west-edge-only I/O — an
  egress relay reachable from the EAST would remove the wrap that costs the
  34-hop `gru_out` failures; or (b) a smaller RMS arm (the 7-cell
  `MovingAverage(32)` is the bulk of the front end); or (c) relay programming
  for >31-hop bus routes, which the router already plans but the build does not
  emit. The offline chain, the goldens, the derived tolerances, and the
  stimulus are all in place and re-usable the moment the geometry gives.

## FeaturePairJoinBlock + SqrtBlock — the ordered two-word rendezvous, and the sqrt tail set free 2026-08-24

Two substrate primitives built together (wave 7) to unblock a two-feature
classifier front end: an ORDERED pair join for the toggle-cell consumer
contract, and a standalone Q15 square root. Both green; 32 + 31 tests.

### FeaturePairJoinBlock — two SEQUENTIAL bursts, not a 2-rail packet

**The problem, restated because it is invisible until you measure it.** A cell
that consumes a FIXED-ORDER pair of words through ONE input port and ONE entry
(the toggle-cell contract — first trigger is word0, second is word1; the
GRUCellBlock's `fin` is the canonical one) cannot be fed by wiring two nets into
that entry. That **builds and routes with `ok=True` and silently produces
garbage**: the toggle reads the two streams as word0/word1 of *alternating*
timesteps and the rate HALVES. None of the existing primitives fit — the
importer's `_elect_join_triggers` declines to arbitrate when the target declares
only an `EntryPoint`; the counting join is explicitly ORDER-FREE; broker
coalescing collapses two nets into N WRITEs + ONE JUMP into the same `in_reg`
(the second overwrites the first); and `DualFloatToComplexBlock` has exactly the
right rendezvous but the wrong OUTPUT SHAPE (a 2-rail packet with one trigger).

**The durable lesson: THE TWO-BURST EMIT NEEDS NO NEW BUILD MACHINERY.** The
expectation going in was `RAW_OUTPUT_HOPS` plus hand-routing. Neither was
needed. `_patch_cell_handoff` — the ordinary single-net source patcher, on both
the abutted and the brokered path — sets **every** WRITE and **every** JUMP in
the cell to the SAME `(hop, dest, entry)`. So a cell that authors

```
MOVE R0, as ; {write:out}  ; {jump:trig}
MOVE R0, bs ; {write:out2} ; {jump:trig2}
```

builds to `WRITE @h→r; JUMP @h→e; WRITE @h→r; JUMP @h→e` — two INDEPENDENT
deliveries of one downstream entry, in program order. Exactly the required
shape, for free. **Two conditions keep that path live, and both are silent
traps if violated** (the block's suite asserts each):

1. **Declare exactly ONE output register.** With `>1` the build classifies the
   cell as a COMPLEX 2-rail source and routes it to `_patch_complex_*`, which
   steers the two WRITEs to CONSECUTIVE registers and fires ONE trigger — i.e.
   it silently converts your two-burst block back into the Dual's packet shape.
2. **Keep the output cell free of internal handoffs and inline `WRITE.CFG`.**
   Otherwise `_output_cell_carries_handoffs` is True and only the LAST
   WRITE/JUMP is patched, leaving the FIRST burst on a stale hop.

More generally: **when you need N deliveries into ONE entry, author N WRITE+JUMP
pairs in a single-rail cell and let the ordinary patcher do it.** The complex
patchers are for N rails into N registers with ONE trigger; do not reach for
them (or for raw hops) for the sequential case.

**DO NOT DERIVE A "KNOWN LIMIT" FROM CODE-READING — MEASURE IT.** A draft of
this block's docstring claimed a limit it does not have: that a downstream
target with >1 input register would break the two-burst contract, because such
a net is dispatched to `_patch_complex_abutment_handoff` (the complex-packet
patcher, which steers rails to *different* registers with one trigger). That
reads convincingly and is WRONG. The patcher sets the hop on every WRITE/JUMP
but the dest only on WRITE index `rail_idx`, and for a net into the consumer's
FIRST input `rail_idx` is 0 while the second burst's WRITE already carries that
same dest from the template — so the outcome is byte-identical to
`_patch_cell_handoff`. Writing the "limit" as an executable guard is what
exposed it: the test failed on its first run, on real hardware, and the
docstring got corrected instead of shipping a false constraint. A limitation
stated in prose and never executed is a guess; the repo's rule that limits are
recorded as guard TESTS is exactly what makes the difference, and it cuts both
ways — the test can prove the limit isn't real.

**ENGINE BUG FOUND AND FIXED — a per-block pass hardcoded to one block's
names.** `_apply_rendezvous_input_faces` (build.py) reconciles a face-locking
block's authored placeholder faces to the geometry the ROUTER actually chose.
It looked up ports `"i"`/`"q"` and data words `face_i`/`face_q` — the
DualFloatToComplex's names — so for ANY other `NEEDS_DISTINCT_INPUT_FACES`
block it was a **silent no-op**: the LOCK kept gating the authored placeholders,
the real arms were barred by the arbiter, and the chain **built and routed
perfectly while emitting ZERO output**. Symptom is indistinguishable from a
datapath bug; the tell is that the *placement DRC is clean* (it was already
port-name-agnostic) while nothing comes out. Fixed by having the block declare
`RENDEZVOUS_FACE_PORTS = ((port, face_word), ...)` in first-accepted order, with
the Dual's names as the fallback and now declared explicitly on the Dual itself.
**Rule that generalizes: a build pass keyed on PORT or DATA-WORD NAMES is a
per-block pass wearing a general pass's clothes — make the block declare the
names, or the second block of that class fails silently.**

**MEASURED SUBSTRATE LIMIT — the arm-overhang depth is 2 (mechanism-wide).** An
arm may run at most TWO timesteps ahead of the other. At depth 3 the surplus
words are **LOST, not queued**: the face LOCK bars the running-ahead arm, the
back-pressure fills its single-outstanding link, its producer wedges, and the
chain emits nothing even after the other arm catches up. Crucially this is a
property of the LOCK-BY-FACE MECHANISM, not of the new block — the shipped,
verified `DualFloatToComplexBlock` hits the IDENTICAL depth-2/depth-3 boundary
in the same chain, and the suite measures BOTH so the limit is a substrate fact
rather than an unexplained new-block failure. Harmless for the intended use
(equal-rate arms off one stream are index-aligned by construction, overhang 0 or
1), but pinned by `test_known_limit_arm_overhang_depth_is_two` so a future
change to link depth shows up as a test result instead of silent data loss.

**Verification note worth copying: every proof is a REAL two-upstream chain.**
No single-block proxy can drive this block (a "sample" is one word on each of two
DISTINCT faces from two SEPARATE blocks), so the suite builds two independently
rate-reducing `KeepOneInN` arms → join on a placed + routed chip and drives ONE
arm at a time, which lets it produce arrival orders the auto-placer never would:
A-then-B, B-then-A, bursty, random over 3 seeds, and a starved arm. The
end-to-end gate then runs the WHOLE chain into the REAL consumer (2 arms → join
→ `GRUCellBlock` → `x16_out`, one 10x12 chip) and asserts the class words are
BIT-IDENTICAL to feeding that same consumer the ordered pairs directly, at the
correct 1:1 pair rate — which is what pins the rate-halving failure mode.
Getting that chain to route needed an anchor-retry loop (auto_pnr is a CP-SAT
search and is not run-to-run deterministic with a 51-cell ring on the die); the
retry doubles as coverage, since different anchors give different arrival-face
geometries.

The MISSING-LOCK mutation is worth the extra thought it took: you cannot simply
delete the lock and re-run (an unlocked build mis-pairs *nondeterministically*).
Instead model the unlocked cell as an arrival-order counter, assert it DIVERGES
from the reference under B-then-A, and assert the real locked block on chip
MATCHES under the same interleaving. The contrast is the proof the LOCK is
load-bearing.

### SqrtBlock — lifting a fused stage out, without letting it drift

`sqrt` existed only INSIDE `RMSBlock`'s fused power+IIR+sqrt datapath, so it
could not take an external power word. The extraction is deliberately a LIFT,
not a re-derivation: the three cells (`norm` → `poly` → `denorm`) and the frozen
coefficients are imported from `rms_block.py`, and a gate asserts word-equality
with `_RMSCoreBlock._sqrt_q15` over all 32768 inputs. **When you factor a stage
out of a fused block, import it and assert equality over the whole domain — a
copy-pasted datapath silently diverges the first time either side is tuned.**

Only ONE line differs from the RMS `norm` cell, and it matters: RMS's input is
an IIR average that is non-negative by construction, while a standalone block
takes a word from ANY producer. A negative word (bit 15 set) would spin the
normalize loop forever, so the guard routes `x <= 0` to the existing `s=30`
sentinel path. **The ISA has no `BR.GT`** (the branch table is C/Z/N/V/P/A/SLT —
see PROGRAMMING_GUIDE §4.3), so the test is the equivalent signed
`CMP ys, one; BR.GE`: `ys < 1` covers zero AND every negative word in one
overflow-correct branch.

**Derived-tolerance practice worth repeating:** the suite MEASURES the bound it
uses. `test_sqrt_exhaustive_error_bound` sweeps all 32768 words, asserts the
error interval is exactly `[-4, +1]` LSB, and `TOL_LSB = 5` is `ceil(|bound|)`
from that. A datapath regression fails the bound test FIRST, so the tolerance
can never be quietly widened to absorb it. Measured against LIVE GR
`blocks.transcendental('sqrt','float')`: max 3 LSB, corr 1.0000, NMSE -83 dB.

**GRC-binding gotcha, new class:** GR's `blocks.transcendental` takes a param
literally named **`name`**, which is structurally inexpressible as a placeKYT
param — the catalog constructs every block as `cls(name=<instance name>, **params)`
and `_derive_params` explicitly SKIPS `name`. Combined with the real reason
(every libm function is a different on-chip datapath), the function is the
block's IDENTITY, and both `name` and `type` (meaningless on a Q15 fabric) go in
`GRC_UNSUPPORTED_PARAMS`. **If a GR block's parameter collides with the block
instance name, it can never be a Kyttar param — say so in
`GRC_UNSUPPORTED_PARAMS` and in the docstring rather than renaming it.**

Also inherited (and re-guarded at source level here): **no `GOTO` in the EXIT
cell.** A `GOTO` assembles to a local hop-31 JUMP and the output-handoff pass
rewrites JUMPs in the exit cell into the external output trigger — the denorm
shift loop would run exactly once and every `s >= 4` output would come out
exactly 2x. `test_exit_cell_has_no_goto` asserts it on the template so an edit
cannot reintroduce it.

### Harness note — the two SERVER-HOSTING tests are unreliable inside a full run

Not a block lesson, but it cost four separate investigations across two agents
during this build, so it is recorded here to stop the fifth.

**Symptom.** A full `pytest verification/tests/` run BLOCKS partway through —
no failure, no output, just a stop. Process state: `State: S (sleeping)`,
`wchan = poll_schedule_timeout`, ~33 threads, and — the signal that actually
distinguishes blocked from busy — `utime` and `voluntary_ctxt_switches` FROZEN
across samples. **Do not use `ps` `pcpu` to judge this**: it is a
process-LIFETIME average, so it still reads ~100% long after the process stopped
doing any work. Reading it as "healthy and computing" is how one of these
investigations went backwards.

**Who.** Exactly two files host a `SimServer` on the fixed port **58950** (the
`.grc`'s baked bind) and then run a real GNU Radio subprocess against it:
`test_examples_grc_userpath.py` and `test_fec_link_example.py::
test_shipped_grc_user_path`. `grep -rln "user_path\|_serve(\|
stop_gnuradio_server" verification/tests/*.py` is the complete membership test.

**Mechanism (what it is NOT, measured).** It is not missing teardown — both
files call `sim.stop_gnuradio_server()` in a `finally`. It is not a `TIME_WAIT`
residue either — `sim_bridge.py` sets `SO_REUSEADDR` on the listener. The
remaining suspect is a LIVE holder surviving teardown between cases: stopping
the server does not necessarily reap a `_run_flowgraph` GNU Radio SUBPROCESS
that is still attached to the socket. Consistent with that, when a run blocks,
`ss -ltnp` shows the port held by **that run's OWN pytest PID** — i.e. this is
SELF-contention within one suite, not only cross-talk from a concurrently
running builder (though a foreign holder produces the same block, and on a busy
machine you will also see the fast form: `OSError: [Errno 98] Address already in
use` at the `SimServer` bind).

**What to do.** Run these two STANDALONE on a quiet machine; they pass there.
For a whole-suite regression signal, run:
`pytest verification/tests/ --ignore=verification/tests/test_examples_grc_userpath.py
--deselect verification/tests/test_fec_link_example.py::test_shipped_grc_user_path`
— DESELECT the single test rather than ignoring the whole `fec_link` file, so its
INV-32 saturated-exactness and mutation gates still run. This is pre-existing
harness flakiness, unrelated to any block: triage it as an artifact, and never
report a block or a build change as regressing on the strength of it.

## GRUCellBlock — the recurrent composite: 51 cells, one closed ring, bit-exact h ON CHIP 2026-08-24

The largest single block in the catalog and the first with an INTERNAL
recurrence over a whole macro-loop: one full GRU timestep (H=4, I=2) plus its
4-class linear readout head, folded as ONE closed 7x8 ring serpentine (the FLL
column-pair fold) with 48 ring cells + an off-ring egress relay + two face-only
ring-closure transits. Bit-exact on chip at BOTH verification levels; 46 tests.
Durable lessons:

- **THE LAYOUT DICT IS A POSITIONAL INDEX, not just a geometry table — put
  every PROGRAM cell first, in `build_cell_programs` order, and the FACE-only
  transit cells LAST.** The router resolves a declared internal handoff by
  `pb.cells[cell_programs_keys.index(dst)]`; `pb.cells` comes from
  `default_layout`. Interleaving a transit at its *ring* position (the natural
  thing to do when the layout function walks the serpentine) shifts every later
  program cell's resolved destination by the number of preceding transits, and
  the block builds, routes, and computes garbage. Symptom when it bit: two
  orientations produced correct h and six did not, with no build error.
- **A cell whose ONLY output is the block's EXTERNAL egress must be the LAST
  program cell.** Its hop is stamped later by the build's egress patcher, but
  until then the router's positional-default branch ("hand this port to the
  NEXT dict cell") is still live for that port — and that default traces the
  ROUTE-TIME faces, which on a CLOSED ring can go the LONG way round. Measured:
  the identity fold traced 6 hops, the two 180-degree D4 orientations traced 44
  and the assembler rejected `Distance must be 0-31` before the patcher ran.
  Found ONLY by the 8-orientation gate; nothing at identity hinted at it.
  Two tempting non-fixes, both measured and both wrong: declaring the port
  `__terminate__` in `internal_connections` makes the cell an internal-handoff
  SOURCE, so `_reassert_internal_forward_faces` restores its authored face and
  KILLS the routed egress (no output at all, the ring stalls with h frozen);
  and moving the relay off-abutment leaves `amx`'s in-program FACE flip aimed
  at a Manhattan fallback that does not follow the ring.
- **Read the STATE, not just the decision — a recurrent block makes this
  cheap and it is the whole gate.** The four hidden words live in the `umB{i}`
  cells' pinned `hs` registers, so `read_cell_memory` gives the on-chip h
  trajectory after every timestep. That is the level where faults show: the
  gate's random-feature class stream is near-constant (mostly one class), and
  three of the first mutants PASSED a decision-only comparison while being
  plainly broken. Two consequences worth carrying: build the mutation stimulus
  by STITCHING real clips of every class (assert `len(set(golden)) >= 3` so the
  stimulus is proven multi-valued), and gate each mutant at the level it can
  actually move — a HEAD-scale fault provably cannot perturb h, since the
  readout is strictly downstream, so asserting it there is asserting a
  falsehood.
- **A trained model can be robust enough that a single perturbed weight word
  changes nothing you can see at the decision level — say so, don't inflate the
  mutant.** One coefficient shifted 600 LSB moved h on 239/240 steps but left
  every clip-level VOTE unchanged on the held-out set. The honest shape is two
  tests: the manifest's "perturbed weights must degrade accuracy" claim made
  with genuinely corrupted weights (all r-gate and head coefficients x3/4:
  37/40 -> 30/40 clips), and the one-word fault pinned at the STATE level where
  it is observable. Likewise, the "per-row head scales corrupt the argmax"
  design rule is real but its NAIVE mutant is a NO-OP for the shipped model —
  all four rows' independently-derived minimal scales happen to equal the
  common one (S = 4) — so the mutant must FORCE the difference and say why.
- **The timestep barrier makes a ~50-cell recurrent macro-loop saturation-safe
  by construction.** `fin` LOCKs its arbiter on the SECOND feature word (the
  lock face is the ring-inbound face, so the h write-back and the unlock
  WRITE.CFG are still admitted while the port corridor is held) and `amx` — the
  chain END — clears it only after the class word has egressed. Exactly one
  timestep is ever in flight, so saturated == per-sample == golden including the
  final h, on the first run, with no restructuring: the documented big-ring
  deadlock class never arises. This is the FFT16 chain-END-unlock rule applied
  at whole-block scale; run the saturated gate EARLY, it costs nothing.
- **Force ONE COMMON scale wherever numbers are later COMPARED or SHARE an
  engine.** r and z share one per-unit sigmoid engine, whose `dshift` is baked
  per instance, so both gates must quantize at one `S_rz`; the four readout rows
  feed one argmax over RAW accumulator words, so they must share one `S_head`.
  Both are derived as `max` over the per-row `scale_schedule` S with the
  post-rounding guard RE-VERIFIED per row at the common S (bump until it holds)
  — the guard is not inherited from the per-row derivation.
- **Split the n-gate row rather than shifting.** `n = tanh(Wxn.x + r*(Whn.h) +
  bn)` needs the reset gate applied AFTER the Whn matmul (the trained/PyTorch
  form; elementwise-first computes a different function and collapses
  accuracy). Holding it as two rows — `u` = `[0,0,Whn[i,:]]` bias 0, `xc` =
  `[Wxn[i,:],0,0,0,0]` bias bn — makes the combine a plain `MULQ(r,u) + xw`
  with no shifts, and the no-wrap guard is checked on the COMBINED budget
  (`sum|u| + sum|xc| + |bias| <= 32767`), not per row.
- **A shared legality gate that hard-codes a "free" cell breaks on the first
  block big enough to cover it.** `test_single_cell_move_rejects_overlap`
  moved a cell to (9, 9) to prove a LEGAL move still succeeds; a 51-cell 7x8
  block anchored at (3, 3) spans x 3..9 / y 3..10, so (9, 9) is the block's
  own `xc2` and the gate failed on the BLOCK for a defect in the TEST. Fixed
  by SEARCHING for a free cell against the chip dims. Worth checking any
  shared suite for fixed coordinates before adding the catalog's new largest
  block to it.
- **Accuracy cost of the 17-entry activation engine, measured end to end:**
  clip 0.9458 / step 0.9132 on the 480-clip held-out set vs 0.9583 for the
  1024-entry-LUT training reference — 1.25 points, inside the 2-point budget,
  and dominated by the tables rather than the MAC quantization. On chip the
  120-clip vote reads 0.9667, IDENTICAL to the offline model on the same clips
  (the DUT is bit-exact, so the only honest comparison is same-clips /
  same-protocol — compute it in the test rather than hard-coding a number a
  dataset regeneration could silently invalidate).

## FFT64/FFT128 under the CHIP-SCALE class — arithmetic DONE, placement blocked by the STAGE-BAND geometry 2026-08-24

The owner un-quarantined FFT64/FFT128 with a policy decision: a transform-scale
block is typically the SOLE OCCUPANT of a die, so for a declared **chip-scale
block class** the perimeter routing-channel reservation and the D4 rotation
requirement are waived, and the only placement contract is that the block's
input and output are reachable from the x16 ports. The class is now implemented
and the FFT arithmetic is built and verified at both sizes; **placement is not
done**, and the reason is a geometry wall the old whole-block cell count could
not see. Report honestly: `needs_human`, with the exact shortfall.

- **The chip-scale flag is narrow by construction.** `KyttarBlock.CHIP_SCALE`
  (default `False`) plus `layout_caps()`: a declaring class gets the full
  panel, every other block still gets `(8, 8)`. `CHIP_SCALE_ORIENTATIONS`
  makes the rotation waiver EXPLICIT — a 10-wide fold cannot rotate on a
  10x12, so the class declares the orientations it SHIPS (identity, mandatory)
  rather than silently skipping the D4 gate. Gated: the flag defaults off, it
  relaxes only its own class, and both FFT sizes still bust the ordinary cap.
- **THE MEASURED FOLD COST IS 9 CELLS, NOT 5 — and the old note SAID its
  number was unattainable.** The prior fit analysis charged the octant fold at
  "2 table cells + steer/prods/rail", explicitly "deliberately unattainable, so
  the wall cannot hinge on the fold-cell estimate". Building it measured 9:
  `seq` (slot counter) → `mcalc` (the `m = M − |r − M|` triangle index +
  trivial detect) → `tab_c` → `tab_d` → `swap` → `sign` → the shipped
  steer/prods/rail. The split is FORCED by the 32-word cell budget — all six
  pairwise merges were tried and every one busts it — and a split-bank DIRECT
  table is strictly worse (its range check busts every cell at every bank size
  tried). **Lesson: an "unattainable lower bound" is not a budget. When the
  bound is doing load-bearing work in a fit decision, build the cheapest real
  cell before trusting the number** — here the correction moved N=64 from 77+
  to 81 and, more importantly, moved stage 0 from inside a band to 3 over it.
- **The 4-way octant steering FACTORISES into three independent decisions,**
  which is what makes it fit cells at all: `swap = o0 XOR o1` (c takes S,
  d takes C), `c sign = o1`, and **`d` is ALWAYS negative**. Every stored
  magnitude is < 32768 (max C[1] = 32610 at N=64, 32729 at N=128), so the
  negates are exactly representable and the fold needs **no saturating
  combine** — and `MUL` by `0xFFFF` (low-16) is an exact two's-complement
  negate, cheaper than a compare-and-branch. `m == 0` — where `C[0] = 32768`
  would be unrepresentable — occurs at EXACTLY the two trivial exponents
  (k = 0, k = N/4), both dispatched structurally, so the tables are never
  indexed there. All of this is asserted exhaustively, not argued.
- **One sequencer stride serves a strided stage.** N=128 needs the fold on
  stages 0 AND 1; stage 1's exponent is `k = 2j`. Advancing the slot counter
  by `2^s` (a build-time immediate, INV-34) lets the SAME 16+16-word octant
  tables serve both — gated, since a wrong stride is exactly the kind of fault
  that would silently corrupt only the second fold stage.
- **THE WALL: a stage cannot spill out of its 2-row band.** In a 2-row
  serpentine of width W, a stage starting at chain index `a` has its `out`
  directly below its `ctl` only when `L = 2W − 2·(a mod W)`; the first stage in
  a band has `a = 0` and must therefore FILL the band. So stages can never be
  merged or split across bands without breaking the @1 write-back — and the
  shipped FFT16 layout confirms it (4 stages, 4 bands, `ctl` at (0,2s), `out`
  at (0,2s+1), no merging). A 2-row band on a 10-wide array holds **20 cells**;
  N=64 stage 0 is **23**. **Corollary I got wrong first and had to check
  against the shipped layout: "merge the two trivial stages to save rows" is
  NOT available.**
- **The second wall is ROWS, and the port row is the trap.** One band per stage
  means 2 rows per stage: N=64 needs 12 rows, N=128 needs 14. `x16_in` (0,0)
  and `x16_out` (9,0) are BOTH on row 0, so a full-width sole occupant gets
  rows 1..11 = **11**. Getting this wrong inflates the budget by a whole row.
  (Empirically the router IS happy to have a block's landing cell sit ON the
  input port cell — FFT16 anchored at (0,0) routes and runs, its `s0_ctl`
  exactly on (0,0) — but a 10-wide block's column-0 `ctl` column cannot claim
  row 0 without colliding with the port, and anchor choice turned out to be
  routability-sensitive in ways only a real built-chip run settles. That is
  precisely why the class's one contract must be gated end-to-end, never by
  inspection.)
- **Cell-shaving does not close it.** Measured spare words: `out` 7 (enough for
  ONE absorbed ring sample → −1 delay cell), `ctl` 4, `gather_tw` 1,
  `sum_leg` 1, `rail` 1. Max reachable saving ≈ 1 cell against a 3-cell
  shortfall. The remaining mechanisms are a 4-row band with a feedback TRANSIT
  COLUMN (so `_apply_internal_feedback` resolves the backward write-back +
  lock-clear `WRITE.CFG` by corridor re-hop instead of @1 abutment — but it
  costs 2 more rows, so it needs the row wall solved too) or the 2-die
  stage-boundary split.
- **N=128 single-die is ruled out with arithmetic, not a shrug:** 110 cells is
  EXACTLY the 110-cell sole-occupant area (zero slack) and it needs 14 rows
  against 11. The 2-die split is the authorized path.
- Cost/shape: `fft_large.py` is a parametric `LargeFFTBlock` (+ `FFT64Block` /
  `FFT128Block`) reusing the FFT16 spine, twiddle and delay cell programs
  verbatim; N=64 = 81 cells, N=128 = 110, every cell resolver-verified inside
  32 words with state pinned (INV-33). Constructing either raises
  `LargeFFTGeometryError` naming the exact shortfall — a LOUD failure, because
  a block that builds but does not route looks identical to a working one until
  you run it. 37 gates in `test_fft64_fit_limit.py` (reworked from the old wall
  test, whose policy-change signal fired exactly as designed).

## ConjChirpMixerBlock + ChirpSyncBlock — the CSS receive spine closes; wrap-vs-saturate is USE-CASE-dependent 2026-08-24

Joint build (QUEUE B9a/B9, CSS track; full cost on ChirpSync, mixer a
zero-cost cross-reference). Dechirp = ComplexMixer NCO front + ChirpGenerator
double accumulator + MultiplyCC saturating tail; sync = a 1-cell
K-consecutive-equal-argmax run detector. Both bit-exact on first sim contact;
the SYSTEM gate runs the whole RX spine (dechirp → FFT16 → mag² → Delay(1) →
argmax → sync) as ONE placed+routed 10x12 chip, saturated, SER 0/1000 at
10 dB. Durable lessons:

- **A parent block's Q15 corner convention does NOT transfer to a subclass
  whose USE CASE changes the operating point — re-derive it.** ComplexMixer's
  wrapping (non-saturating) rail combine is fine under its documented
  |rail| < 1 stimulus contract; the dechirp's PRIMARY input is a full-scale
  unit chirp, so its output rails graze ±1.0 BY DESIGN, and MULQ floor
  truncation (each product ≤ true, ≥ true−1 LSB) pushes a true −1.0 rail to
  −32769 → the wrap sign-flips it full-scale. Measured: the s=4/n=16 dechirp
  turned the exact bin-4 tone into a spectrum with 1/3-peak spurs (every 4th
  sample flipped, exactly the samples where the tone crosses a rail axis) —
  s ≡ 0 (mod 4) symbols hit it constantly, off-axis symbols never. Fix:
  swap the fused mixer cell for MultiplyCC's prods→combine saturating pair
  (V-flag minuend-sign restore). Gate: a wrap-combine mutant golden must
  FAIL, and the diff must be the full-scale sign flip (not a 1-LSB nit).
- **A free-running double accumulator IS the repeating reference** —
  n·(65536/n) ≡ 0 (mod 2^16), so the s=0 chirp reference re-arms its
  frequency word every n samples with no counter, no compare, no reset (the
  generator's wraparound-is-the-cyclic-shift insight, applied to a 1:1
  block: the burst/self-pacing machinery deletes cleanly). Gate the boundary
  return (freq word == 0x8000 at every k·n) explicitly.
- **The INV-20 unlock corridor can be a DIRECT @1 abutment** — folding the
  saturating tail as prods(1,1)+combine(1,0) puts the exit cell directly
  EAST of phase(0,0): unlock_face=WEST, authored @1, NO transit cell in the
  locked variant (13 cells both ways), and I/O co-locate on the top edge for
  free. `XOR satpos, satpos` supplies the WRITE.CFG's R0=0 without a
  dedicated zero word.
- **Composing a streaming FFT with a framewise consumer needs an ALIGNMENT
  DELAY: latency N−1 ≡ −1 (mod N).** FFT16's frames occupy outputs
  [15+16f .. 30+16f] while BinArgmax frames occupy [16g .. 16g+15] — the
  frames STRADDLE, and one argmax frame can contain peaks from TWO adjacent
  FFT frames (ambiguous). ONE extra real-rail sample (DelayBlock(1)) lands
  every argmax frame exactly on one FFT frame; the decode map is then
  s = brev4(index), frame 0 is the deterministic zero-startup frame, and
  frame f+1 carries symbol f (decoding the last symbol needs one flush
  symbol). The no-delay mutant golden must FAIL — pin the framing as a
  system-level mutation.
- **The whole 5-block RX spine fits and routes on ONE 10x12 chip** (mixer
  2x6 at col 0-1, FFT16 6x8 mid-die, the four 1-cell tail blocks along the
  bottom rows; ~60 cells) — auto_route_all + build on the FIRST placement
  attempt, and the saturated queue_words drive of the whole chain is
  bit-exact vs the composed integer goldens at ~40 symbols/0.5 s sim wall —
  fast enough that the ≥1000-symbol SER number is measured with the full RX
  ON-CHIP, not on a golden proxy (boundary: TX goldens + numpy channel).
- **A run detector's counter must SATURATE at K, not count** — `CMP run,k;
  BR.GE locked` BEFORE the increment short-circuits an already-locked run,
  so arbitrarily long preambles can never overflow the 16-bit counter; the
  packed-word output (sign bit = inverted sync flag, value = locked bin,
  0xFFFF sentinel) keeps the block 1-output and collision-free since legal
  indices are 0..32767.
- **Shared-registry honesty:** the shared REAL_1IN saturation stimulus has
  no equal-adjacent pair, so for ChirpSync it exercises only the sentinel
  path — say so in the registry comment and gate the LOCK-asserting
  saturated case bespokely (repeated-index stimulus, ≥6 locked frames
  premise asserted).

## FFT64Block / FFT128Block — the single-block PLACEMENT wall, quarantined at the FIT CHECK 2026-08-23

The first queue item stopped BEFORE authoring, on arithmetic alone — and that
is the correct outcome, not a failure: the fit check was run first (as the
dispatch required), it failed decisively, and the wall is a publishable
architecture datum. Both rows are `needs_human`; the executable form of the
finding is `verification/tests/test_fft64_fit_limit.py` (7 tests, green).

- **The wall: a 64-point single-block streaming R2SDF cannot fit the 10x12.**
  Floor from SHIPPED MEASURED constants only — per-stage spine 7 cells (ctl +
  4 RHE leg cells at 24-25/32 words each + gather + out), ComplexDelayLine
  density 5 complex samples/cell, TwiddleMultiply direct chain 5 cells — gives
  **77+ cells** for N=64 (42 spine + 15 delay/relay + 10 for the two shipped
  FFT16-shape stages + 5 for the P=16 stage + 5+ for the octant-folded P=32
  stage) and **102+ for N=128**. The same accounting REPRODUCES the shipped
  FFT16Block (floor 43 + its one documented layout-padding delay cell = 44) —
  calibrate a floor formula against a shipped block before trusting it.
- **The cap is 8x8 = 64 cells, and the D4 gate is what makes it bind BOTH
  dims.** INV-9's ≤8-across derives from the 10-wide axis (bus channel each
  side); the mandatory 8-orientation invariance gate rotates a block 90°,
  swapping its dims — so a "tall" 8x10 fold is not an escape (rotated it is
  10 wide = zero channels = unroutable). Banded at 8 wide, FFT64 needs 11
  rows on the 12-tall chip: no top/bottom channels even at identity. The gap
  is structural, not tunable: even charging ZERO cells for all N=64-specific
  twiddle machinery leaves 67 > 64.
- **The wall is GEOMETRIC, not numeric — the octant fold is proven.** The
  big-N twiddle growth path (two octant tables, COS and SIN over (0, π/4]:
  8+8 words at N=64, 16+16 at N=128, one cell each) reconstructs every
  non-trivial `round(32768·x)` twiddle word pair BIT-EXACTLY — asserted
  exhaustively at both sizes against the shipped `quantize_twiddle`, with
  INV-4 negatives (wrong-quadrant-sign and un-reflected-quadrant folds break
  the equality). The fold is `o = k // (N/8)`, `m = |k − round_{N/4}(k)|`,
  steering per octant (+C,−S)/(+S,−C)/(−S,−C)/(−C,−S); the π/4 boundary slots
  (k = N/8, 3N/8) quantize identically through cos and sin float paths (both
  23170), so the boundary octant assignment is free. Table STORAGE was never
  the blocker — do not let the quarantine be misread as "tables don't fit".
- **Pre-build estimates in planning notes are not budgets.** The manifest
  carried "~49-57 cells" (N=64) and "~65-75, should fit" (N=128) — both wrong
  by ~50%, written before FFT16 existed. Once ONE calibrated instance ships,
  re-derive every scaling estimate from its measured per-stage costs before
  starting the next size. Ten minutes of arithmetic saved a doomed multi-day
  build.
- **The unblock is a product-shape decision, so it is a HUMAN call:** split
  the pipeline into 2-3 chained stage-blocks wired in GRC (each under the
  64-cell cap; needs an inter-block contract for the per-stage serialize-LOCK
  handoff and a composite gate), or a bigger array. The guard test flips
  (starts failing) if `MAX_CELLS_ACROSS` or `SAMPLES_PER_CELL` ever grow —
  that is the un-quarantine signal.

## FFT16Block — 16-point streaming R2SDF FFT: the stop-gate composite, PASSED 2026-08-23

The FFT track's hard gate: a 44-cell, 4-stage streaming radix-2 single-path
delay-feedback FFT (DIF, delays 8/4/2/1, 1 complex in → 1 out per trigger,
latency 15, output in BIT-REVERSED bin order, scale FFT/16), composed from the
shipped builders exactly as planned — the R2Butterfly RHE leg programs, the
TwiddleMultiply fetch/steer/prods/rail/emit cells, and the ComplexDelayLine
segment cells — bit-exact (tol 0) on-chip against the transcribed streaming
integer golden, saturated == per-sample, all 8 D4 orientations, GRC
import→auto-P&R→build green. One real substrate bug found and fixed on first
sim contact; durable lessons:

- **THE ROUTE-TIME FACE RULE (the bug, and the durable design rule for any
  multi-hop-write composite):** internal write/jump DISTANCES are resolved by
  TRACING each cell's fwd_face in the cell map — but at ROUTE time a cell's
  face comes from its LAST-listed internal connection **when that dst is
  physically adjacent**, else from the dict-NEXT cell; the authored
  default_layout faces are applied only later in the build. A cell whose last
  connection targets an ADJACENT NON-SUCCESSOR (the diff leg sitting directly
  above the delay-line push cell) gets a route-time face pointing off the
  serpentine → every trace crossing it fails → those writes silently resolve
  to MANHATTAN hops (wrong for any folded path) and land mid-chain in other
  cells' registers. Symptom: stages whose geometry avoided the adjacency were
  bit-exact; the two that didn't produced stale/garbled rails. RULE: for
  every cell, its last-listed connection dst must be its chain successor OR
  non-adjacent; audit `(cell, last-dst)` adjacency when authoring any fold
  (the FFT16 chain runs ctl → sumi → sumq → diffi → diffq for exactly this
  reason, and prods lists p4 before p1..p3).
- **The re-timed R2SDF ring: hold the delay line's LAST sample in the stage
  controller's state.** The textbook stage pops its D-deep line and pushes in
  the same step — circular within one trigger on a fabric. Keeping D-1
  samples in ComplexDelayLine-style segment cells and the emerging sample in
  the ctl's (ai, aq) STATE pair (written back by the stage's out cell at the
  END of the wavefront) makes the whole stage a linear serial chain with one
  backward data edge — provably the same schedule (asserted against the
  spike's cycle-accurate model, 3 transcriptions bit-equal).
- **Per-stage serialize-LOCK with the unlock AFTER the packet handoff.** The
  a-write-back races the next sample (INV-19 by construction), so every stage
  locks its ctl on dispatch. Two ordering rules proven out: (1) the stage's
  OUT cell must emit its packet BEFORE the write-back+WRITE.CFG **in wait
  terms** — i.e. the unlock only fires once the packet is accepted — or a
  back-pressured out cell can have its inputs clobbered by the next sample;
  with the wb+cfg textually first but the packet writes LAST IN PROGRAM
  ORDER (the complex-egress patchers patch the last N data writes and skip
  config writes), a top-of-program yi/yq SNAPSHOT plus the lock's
  one-in-flight guarantee closes the same hazard. (2) The unlock in the
  chain-END cell bounds pending samples to ONE; unlocking from a mid-chain
  cell allows two and reintroduces the clobber. The stacked-band layout (out
  directly below ctl, next ctl directly below out) makes the write-back,
  unlock, and inter-stage packet all @1-adjacent — no transit cells, no
  _apply_internal_feedback tracing needed (the backward @1 edges resolve
  through the router's manhattan fallback, correctly and orientation-
  invariantly).
- **Fill vs butterfly paths of UNEQUAL length are safe UNDER the lock.** The
  butterfly path bypasses the 5 twiddle cells (diffq jumps gather's pass
  entry @6, transiting them); the fill path runs through the tables. With
  one sample per stage in flight, no overtaking is possible — the
  equal-path-length rule TwiddleMultiply needed standalone is subsumed by
  the stage lock.
- **The bfly path enters the twiddle gather at its `id` entry with the sum
  legs writing (si, sq) into the same yi_in/p3 registers the trivial path
  uses** — multi-source input ports (two writers, one register, different
  modes) resolve fine because each src's WRITE is patched independently; no
  extra entry, no extra cell.
- **Counting idiom:** `AND cnt, D` (D a power of two) IS the half-period
  selector — Z set = fill; free-running 16-bit wrap is exact (2^16 ≡ 0 mod
  2D). Dispatch on the flags mid-program (BR.NZ +2 / jump / BR.Z +1 / jump —
  JUMP preserves flags), then increment; the twiddle fetch pointers advance
  only on fill entries so they stay in slot-lockstep forever with no reset
  logic.
- **SNR floor honesty (measured, reported, disclosed):** sine_fs 91.8 dB,
  noise −6 dBFS 78.8, two_tone 77.0, impulse 71.0, noise −26 dBFS 58.8 on
  the gated stimuli (pooled 3 frames) — but the −26 dBFS class's CONVERGED
  pooled SNR is ~57.9 dB: the pinned 58 dB floor sits AT that weakest
  class's per-trial dB-MEAN (the spike's 58.31/min 55.67), and dB-domain
  averaging reads ~0.4 dB above honest power-pooling. Gate that class at its
  seed-variance floor (56 dB), assert+report the measured value, and say so
  loudly — do not reroll seeds until 58 appears. When pinning a floor from a
  spike, pin the POWER-POOLED statistic.
- **Saturation evidence without a visible rail:** the FFT's one reachable
  butterfly clamp (RHE diff tie +0x7FFF/−0x8000) never surfaces as a raw
  ±full-scale OUTPUT word (later stages halve it) — prove it with an
  instrumented golden (tie counter > 0 on a crafted 8×(+FS) / 8×(−1) frame)
  plus a WRAP mutant that must diverge, then bit-exactness transfers the
  proof on-chip.
- Cost: 44 cells / 120 (7×8, both dims ≤ 8), zero transit cells; per-stage
  14/14/8/8 = ctl + 4 RHE legs (+ twiddle chain ×2 stages) + gather + line
  segments + out. Build+sim toolchain fast enough that the whole 36-test
  suite (6 stimulus classes × 4 frames on-chip + mutants) runs in ~2 s.

## ChirpGeneratorBlock + ChirpSymbolMapperBlock — CSS modulator pair; the self-paced return kick + the LOCK bit-0 correction 2026-08-23

Joint build (QUEUE B6/B7, CSS track; full cost on the generator, mapper a zero-cost
cross-reference). Generator = 1 raw symbol word in → n complex chirp samples out
(BIT-EXACT chip vs the integer golden: exhaustive symbols at n=32, spot to n=256,
saturated, 8/8 D4 full-burst, abutted-chain regression). Mapper = PackKBits
re-parameterized (see below). Durable lessons:

- **The 16-bit wraparound IS the mod-BW cyclic shift.** The CSS chirp's frequency
  wrap at +BW/2 needs NO compare: the freq word (s·(65536/m)+0x8000, +65536/n per
  sample) wraps mod 2^16 exactly at the band edge. Same trick counts the samples:
  `cnt += rate` wraps to 0 after exactly n kicks — no count constant, no CMP.
  Gate the wrap with a saturate-instead-of-wrap mutant golden (it must FAIL on a
  symbol near m−1, which wraps within its first samples).
- **A 1:n burst generator on the NCO datapath CANNOT loop locally — it must
  SELF-PACE via a backward return kick.** Looping the sweep cell floods the NCO's
  reconvergent fan-in (the proven INV-20 deadlock, now internally generated). The
  working shape: the sweep cell emits ONE sample per activation; `emit` fires a
  backward JUMP-only kick to the sweep's second entry (`iternext`) AFTER the yi/yq
  pair has left the cell — strict one-sample-in-flight, paced by the output-port
  drain. Kick placement matters twice: (a) BEFORE yi/yq → 2-sample co-residency
  (deadlock class); (b) as a data WRITE after yi/yq → the exit patchers rewrite
  the highest-address WRITE into the output corridor and the iteration dies. A
  bare JUMP after the writes threads the needle: the data-write tail stays the
  canonical complex-egress shape, and a JUMP-only kick reliably re-fires a full
  program cell (the INV-20 "trigger-only relay" caveat is about relay CELLS, not
  jump delivery).
- **Backward internal JUMPs are now first-class in the build** (`build.py
  _apply_internal_feedback` step 3): a `(src, port, dst, entry)` internal_jumps
  entry with dst EARLIER in dict order is re-hop-patched from the placed corridor
  (transit trace, else the NEW 1-hop direct-abutment fallback) and RECORDED as
  `(src_pos, ("jump", entry))` so `_set_cell_hop1` PRESERVES it — the routed-exit
  pass otherwise rewrites EVERY exit-cell jump to the consumer's entry (measured:
  1 sample per symbol, silently). The restore targets the HIGHEST-address JUMP
  (the kick is authored last), since its dest may already be clobbered. Inert for
  every prior block (none declares a backward internal jump). Regression:
  test_chirp_generator.py::test_kick_survives_abutted_exit_defaulting.
- **THE LOCK CONFIG ENABLE READS BIT 0 — "any nonzero" is WRONG** (INV-19 trick
  #2 corrected in place). `MOVE [LOCK], R{rate=0x1000}` left the sweep cell
  unlocked: saturated symbols barged into mid-flight iterations and the run
  deadlocked with 1 pair out; A/B-measured (0x4000 fails, 1 locks). Spend the
  `one` word (or reuse a data word whose bit 0 is set).
- **The 33-entry-table grid interacts with the chirp sizes:** for n ≤ 128 every
  phase is a multiple of 512 (= the table grid) so the interp error vanishes —
  SNR vs the ideal float chirp ~91 dB; n = 256 hits half-grid phases and shows
  the interp-limited regime (73.4 dB measured RMS; worst-case ~11 LSB). Report BOTH; an
  SNR measured only at n ≤ 128 would overstate the block fourfold.
- **Phase-continuity convention pinned CARRY (never reset)** — zero instructions,
  phase-continuous TX, invisible to the magnitude CSS receiver. With n even and
  m | n each symbol advances the carried phase by exactly π, so the carry-vs-reset
  gate is a robust sign flip on odd symbols.
- **Harness: `run_block_dut_rate` still had the INV-1 manhattan hop** — the
  180°-family D4 orientations of the single-real-rail generator returned ZERO
  output until the corridor-accurate landing derivation (input_landings → route
  length → manhattan) was ported from `run_block_dut`. Same masquerade as the
  historical "NCO anti-orientation failure": suspect the harness hop first.
- **ChirpSymbolMapperBlock decomposes to trivial reuse — say so:** it IS
  PackKBitsBlock with m = 2^k and the GR uint8 OUTPUT cap lifted (raw 16-bit
  symbol word; k = 10 proven on-chip). For m ≤ 256 it is gated against the LIVE
  `pack_k_bits_bb` (a real GR golden); the dtype gate needed a documented
  `_DTYPE_DEVIATIONS` entry (byte-in/short-out — the lifted cap is the deviation).
  MSB-first pinned by an LSB-first mutant golden.

## SigmoidBlock + TanhBlock — Q15 activations, the runtime-patch-slot unfold 2026-08-23

Joint build (QUEUE A1/A2, GRU track; shared module `activation_blocks.py`, one
commit — the RMS/RMSCF convention). No GNU Radio counterpart: goldens = numpy +
a TRANSCRIBED canonical integer reference; the design (16-interval table +
interp, 17 Q15 entries, canonical domains ±8/±4, `dshift = S − k` folded into
the two shift immediates) was pinned by a completed numeric spike and is
implemented bit-exactly — first on-chip run matched on every probe, all four
dshift values, zero bug iterations. 48 tests + 3 fleet gates. Lessons:

- **When a cell is 1–2 words over budget, look at the RUNTIME PATCH SLOT
  (BlockInterleaver `store`-cell idiom) before redesigning the split.** The lut
  cell (17-entry table + 2 LOADs + interp + sign unfold) is arithmetically 33–34
  words in every operand encoding of the unfold (`(y^msk)+adj`, `s·y+c`, …) —
  the sign costs TWO sample-dependent operands because `0x8000−y` is affine, not
  linear, in y (provably NOT expressible as `s·(y+k)` mod 2^16). Delivering the
  sign as ONE pre-assembled INSTRUCTION word (pos: `MOVE p, R0` no-op — also the
  authored slot content; neg: `SUB negop, R0`) into an input Port pinned at an
  instruction address makes the unfold cost zero operand registers: 31/31 words,
  every address purposeful. The patch rides the normal 4-word operand packet, so
  it is orientation-clean (no faces) and saturation-clean (proven bit-exact
  pipelined). Pair it with the addr→R0 accumulator delivery (`LOAD R0` consumes
  it as instruction 1) so R0 is a landing slot, not a wasted word.
- **Two shifts replace an AND-mask: `frac = ((mag << (5+d)) & 0xFFFF) >> 1`.**
  The left shift flushes the index bits mod 2^16 — no mask data word. For
  `dshift < 0` a third shift (`>>5, <<4`) also flushes the discarded low bits;
  proven bit-exact to the canonical mask-and-shift form exhaustively.
- **For `dshift > 0`, ONE unsigned compare replaces clamp-at-32767 + index
  clamp:** `mag = min_u(|v|, 2^(15−d))` (CMP borrow + `BR.C`) lands exactly on
  idx=16/frac=0 and absorbs |−32768| for free (the NOT/ADD negate leaves 0x8000,
  which the unsigned compare orders correctly). For `dshift ≤ 0` the index clamp
  is unreachable (mag ≤ 32767 ⇒ idx ≤ 15) — omit it and keep the V-flag negate
  clamp instead. Output equivalence of the two formulations was proven over all
  65536 words × 8 dshift values BEFORE any repo code was written; that
  half-hour of pure-python modeling is why the chip matched on the first run.
- **The dshift-invariance claim in a spike is about MAX error; re-derive RMS.**
  At `dshift < 0` the pre-shift discards |d| input bits, so the
  input-quantization RMS floor rises (sigmoid 0.0010 → 0.0015, tanh 0.0021 →
  0.0029 at d = −1) while max error stays pinned (0.0030/0.0060). The gate pins
  BOTH, per sign of dshift — deriving the bar, not tuning it.
- **Sigmoid's `0x8000 − y` unfold is wrap-exact** (y ≥ 16384 ⇒ result in
  [1, 16384], no saturation path), and `+0x8000 ≡ ^0x8000 (mod 2^16)` is the
  identity that keeps such affine unfolds one instruction.
- The queue note's "arbitrary input-scale parameter" resolved to integer
  `dshift` (in_scale = 2^dshift): powers of two ONLY is a genuine hardware
  limit (INV-34 immediate shift counts), documented in both bindings; range
  [−4, 10] raises outside.

## R2ButterflyBlock + TwiddleMultiplyBlock — radix-2 FFT primitives, the first TWO-complex-output block 2026-08-23

Joint build (the RMS/RMSCF convention: one shared module
`blocks/fft_primitives.py`, one commit; TwiddleMultiplyBlock is the zero-cost
cross-reference). Numerics were PINNED by a completed FFT design spike —
implemented, not redesigned; the bit-exact integer reference helpers
(`rhe_half_sum/diff`, `twiddle_cmul_ref`, `quantize_twiddle`, `mulq`,
`sat_combine`) live at module level so the future composite streaming-FFT block
imports the SAME golden + the cell builders (the `cordic_blocks` consumer
pattern). Both blocks EXACT (tol 0) on first sim contact once placed; all the
real work was substrate/toolchain, catalogued here.

**R2Butterfly (8 cells, 2x4 serpentine, fully serial, counting join):**

- **The RHE (round-half-to-even) halving fits comfortably — no `mixed`
  fallback needed.** 16-bit-safe fabric mapping (the 17-bit sum is NEVER
  materialized): sum leg `k = ((a AND b) AND 1); MACQ a,0x4000; MACQ b,0x4000`
  (11 instructions), diff leg `MULQ a,0x4000; SUB c0; MSUQ b,0x4000` with
  `c0 = (NOT a AND b) AND 1` (16 instructions incl. saturation); both legs
  finish `corr = ((a XOR b) AND k) AND 1; ADD k`. MULQ/MACQ/MSUQ **by 0x4000
  IS arithmetic-shift-right-by-1** (floor) — the ISA has no ASR (SHR is
  logical), and this idiom is 1 instruction + one shared data word.
- **A one-instruction exact saturating clamp for the RHE tie:** the ONLY
  reachable overflow is `k=+32767, corr=1` (diff leg, a=+0x7FFF b=-0x8000), so
  `BR.NV +1; MOVE R0, k` restores the exact saturated value — no
  SHR/ADD-satpos rail rebuild needed. The SUM leg provably cannot overflow
  (max v=65534 is even ⇒ corr=0); gate that with an exhaustive corner test,
  do not just assert it in a comment.
- **TWO complex output pairs on separate cells is now a SUPPORTED shape, and
  it took three build/import changes** (all no-ops for single-output blocks):
  1. `build._net_source_exit_cell`: the per-connection exit-face/handoff
     patches now land on the NET's own output cell (`route[0]`, which every
     router already anchors at the net's source-port cell) instead of the
     block-level `pb.exit_cell` — pre-fix, the second net's patch REWROTE the
     first output cell's WRITE/JUMP with the other route's hops (zero egress).
     Applied in BOTH patch sites (`_apply_routes` and
     `_apply_port_route_faces_and_hops`).
  2. The complex-egress `base_tag` sibling scan groups by EXIT CELL, not by
     block — a block-wide min mis-based the second pair's tags AND skipped its
     patch entirely (`dest != block-wide base`).
  3. The GRC importer's numeric-index pair collapse now works on the OUTPUT
     side too: `[bfly, '1', ...]` resolves to the SECOND pair's I-half
     (`do_i`), indexing over I-HALVES ONLY — the out side also exposes
     per-pair TRIGGER ports (`so_trig`), which are wire-protocol, never
     GRC-indexable.
  Declarations on the block: `output_cell_ids()` (plural, portmap),
  `output_cell_id()` = the mid-chain tap cell (the carries-handoffs flag →
  last-N-writes patch, the order-4-Costas `qpd` idiom), `output_face_addr()`
  for the tap cell's dual-face word (build guards it to the block-level output
  cell only), `interface.output_registers=[0, 1]` (complex-pair egress).
- **An UNWIRED output pair is a live hazard, not a no-op.** Its default-
  resolved `@1` WRITE/JUMPs fire into whatever neighbours the layout leaves
  there; observed failure modes: (a) a jump into a corridor cell re-emitting
  stored words (phantom extra port words, DATA-DEPENDENT — some stimuli
  "worked"); (b) a mutual `@1` jump ping-pong between two corridor/broker
  cells = a self-sustaining livelock that ate 4+ms of sim time (the INV-20
  `__terminate__`-into-corridor shape, reachable from ANY dangling trigger).
  A StreamSplitterBlock "dump" consumer just moves the dangling output one
  block downstream — same hazard. THE FIX: wire BOTH outputs (the DUT runner
  always does; the GRC docs say to terminate unused outputs).
- **Per-rail `out_tag`s make a multi-pair port gate order-free.** The two
  packets' arrival interleave at `x16_out` legitimately varies with corridor
  length (orientation runs reorder [di,si,dq,sq] ↔ [di,dq,si,sq]) — pin
  NOTHING about order; tag the four rails 0..3 (`run_block_dut_complex2_dual`,
  reading `read_port_words_timed`'s dest) and compare demuxed streams.
- **Independent-reference discipline for a Python-golden block:** the gate
  cross-checks the block's 16-bit-safe decomposition against a separately
  transcribed true-17-bit RHE (`k = v >> 1; k + ((v & k) & 1)`, numpy int64)
  exhaustively over corner words + 20k random pairs, so the formula identity
  is PROVEN, not assumed; the half-up and wrap mutants FAIL at the tie points.

**TwiddleMultiply (6 cells, 2x3 serpentine, fully serial):**

- **Kind dispatch as ENTRY CHAINS, not per-cell branches.** One CMP against
  the C-table sentinel in the `steer` cell; from there the path identity
  travels as WHICH ENTRY each downstream cell is jumped at (`prods.mul` vs
  `prods.triv`, …, `emit.mul/id/mj`). Downstream cells carry straight-line
  per-kind code with NO re-dispatch (except one free `SHR #15` sign test on
  the forwarded d word) — this is what fits 3-way trivial special-casing +
  4 MULQs + tables into 32-word cells. Every kind transits EVERY cell
  (trivial slots run pass-through entries), so path lengths are equal: no
  reconvergent fan-in, no overtaking under saturation, no serialize-LOCK.
- **Sentinel table encoding buys the kind table for free:** 0x8000 in the C
  table marks a trivial slot (it can never be a legal non-trivial coefficient
  — the block RAISES on any value quantizing to ±32768, incl. W=-1); the D
  word then disambiguates identity (0) vs -j (sign bit set). Two tables, one
  per fetch cell (each with its own lockstep LOAD pointer), period P ≤ 12.
- **Keep R0 free by landing inputs at R1+ (the CORDIC precedent):** the fetch
  cells then LOAD/forward without an R0-save; with data words present the
  INV-33 no-data-words state-allocation hazard cannot trigger (state still
  explicitly pinned).
- **`quantize_twiddle` uses np.round (half-even) and full 32768 scale** — the
  documented N=16 words (30274/-12540, 23170/-23170, 12540/-30274) are gated
  verbatim, and the 0x7FFF-as-one mutant (multiply the identity slots by
  0x7FFF) FAILS, proving the structural pass-through is what ships.

**Shared:** both blocks' budget tests use the REAL rule
`max_data_address + n_instr ≤ 31` (the resolver packs instructions at
`31 - instr_count` with R31 auto-HALT — the plain `instr + regs ≤ 32` count
passes cells the builder rejects). Placement-legality, orientation (shared
gate for the twiddle; bespoke dest-demux 8-D4 gate for the butterfly),
saturation (shared COMPLEX_2IN2OUT for the twiddle; bespoke dual-runner
pipelined gate for the butterfly) and the GRC bindings/import gates are all
green; full verification suite green after the build/import changes.

---

## DotProductMACBlock — the correlator (fresh-vector) MAC pattern + the post-rounding headroom guard 2026-08-23

Fixed-coefficient dot product over a K-element vector (weighted sum + bias, K:1
rate-reducing) — a placeKYT-native primitive with no stock GR counterpart
(numpy-golden pattern, like Crc16Block). Bit-exact on-chip on the first build;
59-test suite green. Durable lessons:

- **The correlator pattern is NOT the FIR pattern — and the gate must prove
  it.** K consecutive samples form one FRESH vector -> one output; no delay
  line, no sample aging. Structurally this is PackKBits (count + accumulate +
  emit-and-rearm), not FIR (shift line + full MAC per sample). The
  `test_fresh_vector_no_sample_aging` gate pins it: feeding [v1, v2] must give
  [dot(v1), dot(v2)] exactly — an accidental delay-line implementation mixes
  v1 into v2's output and fails. Cheap and worth copying for any future
  block-framed (vector-in) primitive.
- **`idx` doubles as the LOAD address.** With coefficients at addr 1..K in
  natural order, a single up-counter (init 1) is BOTH the position counter and
  the LOAD-indirect address — `LOAD R{state:idx}` fetches the right
  coefficient with zero address arithmetic; `one` doubles as the idx reset.
  MAC cell = 14 instr + (K+3) data + 3 state, K=7 fits at 28/32.
- **Snapshot the input BEFORE the LOAD.** The sample lands in R0 and LOAD
  writes R0 — `MOVE R{state:xs}, R{in:sample}` must be instruction 1 or the
  sample is destroyed. (`MULQ R0, R{state:xs}` straight off the LOAD result is
  fine — R0 is a legal MULQ operand, the nlog10 idiom.)
- **INV-13's scale schedule, correlator form, with the POST-ROUNDING GUARD:**
  S = max(0, ceil(log2(sum|c| + |b|))); store round(v·2^-S·32768) for coeffs
  AND bias; if sum|q| > 32767 after rounding, bump S and requantize. The guard
  is LOAD-BEARING and trips in practice: sum|c| == 1.0 exactly ALWAYS trips it
  (each 0.25 -> 8192, sum 32768), and a float-sum-<1 set trips it via round-up
  ([8191.51/32768]*4 + tiny). Crafting a set that actually WRAPS the unguarded
  16-bit accumulator is subtler than expected: positive overflow is unreachable
  (truncation eats ~1 LSB per product), and the -32768 rail means sum|q| ==
  32768 does NOT wrap — you need sum|q| >= 32769 (round-up on >= 5 stored
  words) driven by a full-scale 0x8000 vector; the mutant then sign-flips
  (+32767 for a true y = -1.0). Reusable recipe for any guard-removal mutation.
- **Restored mode's saturating <<S does NOT fit beside the MAC.** The FIR
  bias-and-shift restore (~12 instr + 2 data + 1 state) + the MAC walk + K
  coefficients busts 32 words for K > 2 — so restored/S>0 is a 2-cell
  `mac -> restore` row (the nlog10 2-cell exemplar: named cells,
  internal_connections + internal_jumps, {write:acc_fwd}/{jump:trig} on the
  mac, restore is the last/output cell). S == 0 restores nothing: same single
  cell as raw, and `restored == raw` is gated word-for-word. Param-dependent
  cell_count (mode + derived S) is fine — FIR precedent; both shapes go into
  orientation + legality + saturation registries.
- **The raw/restored contract doubles as the metadata gate.** raw emits the
  UNRESTORED word (= y/2^S) and exposes S as read-only `scale_shift` (+
  quantized_coefficients/quantized_bias) for downstream consumers that fold
  2^S into their own shift immediates (the nlog10 db_scale convention).
  `clamp(raw << S) == restored-mode on-chip output` is asserted on-chip —
  the contract a downstream instance depends on, tested as such.
- Derived float tolerance: (K+1) stored-word roundings (≤0.5 LSB each at
  |x|≤1) + K truncating MULQs (≤1 LSB) ⇒ ≤ 1.5K+0.5 LSB of the SCALED domain
  (measured well inside). Bit-exact gate (tol 0) vs process_reference_q15 is
  the primary bar; the float gate only pins the scale convention.

## BinArgmaxBlock — framewise argmax, the signed running-max in one cell 2026-08-23

Framewise argmax (tier 2, QUEUE B8 / CSS track; no GNU Radio streaming counterpart
— golden = `numpy.argmax` per frame, first-occurrence ties). Rate-REDUCING n:1: per
non-overlapping frame of `n` signed Q15 words, ONE raw index word (0..n-1). 38
tests, EXACT tol 0, first on-chip run matched the golden (no bug iterations).
Lessons:

- **A signed running-max compare MUST use the SLT branch (`CMP a, b` +
  `BR.GE`/`BR.LT`), never a bare N-flag test.** `CMP maxv, x` computes a 16-bit
  difference that OVERFLOWS for opposite-sign pairs wider than 15 bits (the re-arm
  sentinel −32768 vs any x ≥ 0 included — i.e. the FIRST compare of every frame on
  magnitude inputs), and N alone then orders them BACKWARDS. SLT = `N ^ V` is the
  overflow-corrected signed less-than (guide §4.7) and `BR.GE` gives the
  strictly-greater update + FIRST-occurrence ties in a single branch:
  `CMP maxv, xs; BR.GE skip; <update>`. Proven on-chip with ±full-scale frames
  (`[0x8000, 0x7FFF, 0, 0x8000] → 1`, both orders, and −32767-beats−−32768).
- **Record the frame position as the DOWN-counter snapshot, not an up-counter —
  one counter runs the whole frame loop.** The Crc16 frame down-counter (`SUB cnt,
  one; MOVE; BR.NZ done`) already yields the position for free: `cnt` before
  decrement equals `n − i`, so the update path stores `cm = cnt` and the emit path
  recovers the index as `n − cm` (one `SUB nfrm, cm` straight into R0, WRITEs
  immediately). No second position register, no per-sample compare against `n`;
  16-bit wraparound keeps `n − cm` exact through n = 32768 (`0x8000` down-counter).
  Result: 14 instructions + 3 data + 4 pinned states — far inside the 31-word
  budget (the ZCR R31 lesson pre-checked).
- **The −32768 re-arm sentinel + argmax-register re-arm to index 0 make the
  all-equal and all-minimum frames correct with NO special case:** an all-(−32768)
  frame never fires the strictly-greater update, and the re-armed `cm = n` emits
  `n − n = 0` — exactly numpy's first-occurrence answer. Pin BOTH rails as edge
  frames (all-0x8000 and all-0x7FFF → index 0).
- **Pin the GOLDEN's tie convention as its own test** (the INV-26 spirit for a
  numpy golden): `test_numpy_golden_first_occurrence_tie` asserts
  `np.argmax([5,5,5]) == 0` etc., so the golden is PROVEN to encode the pinned
  contract before the DUT is held to it — and a numpy behavior change would
  surface as a loud gate failure, not silent drift.
- **Raw-word output (an INDEX, not a Q15 sample) follows the crc16 convention
  end-to-end:** GRC yml output dtype `short`, marker `out_dtype=np.int16`,
  compare via direct word-list equality (indices ≤ 32767 also survive the
  `/32768` float round-trip for `compare_against_grc` reports). Document the
  ×32768 rescale for value scopes (blank-scope contract).
- The no-reset-between-frames mutant needs a stimulus whose FIRST frame holds the
  global maximum (mutant then emits 0 for frame 2 where the truth is ≠ 0); the
  tie-flip (>=) mutant needs duplicate maxima; the counter-one-early mutant is
  caught by phase (`outputs[n−2] is None`) AND value on a random stimulus.

---

---

## ComplexDelayLineBlock — multi-cell distributed complex delay, ON-FABRIC to depth 64 2026-08-23

The streaming-FFT track's critical enabler, and the answer is the good one: a
**multi-cell on-fabric complex delay line works, bit-exact, to depth 64 (13
cells), with NO engine wall and NO SRAM-panel fallback needed anywhere in the
supported range** — every depth 0..64 is a pure cell chain, so an FFT stage's
delay memory is genuinely "on the chip". `out[n] = in[n-depth]`, (0,0) zero
prefill, EXACT tol 0 vs an independent numpy golden (+ a live GR
`blocks.delay(gr.sizeof_gr_complex, D)` anchor). 43 tests + the three shared
gates (saturation / orientation / placement legality), first-build clean at
every depth probed. Lessons:

- **The ComplexFIR forwarding idiom minus the MACs IS a distributed delay
  line, and the chain adds NO extra sample delay.** Cell m holds an L_m-sample
  segment per rail (`di*`/`dq*` + ONE shared `osave`); per trigger it captures
  its oldest I BEFORE the shift, shifts + ingests, forwards the oldest to the
  next cell's xi, repeats for Q (osave is free again after the I forward), then
  JUMPs. The handoff rides the SAME trigger wavefront (like the FIR's systolic
  sample forwarding), so total delay = Σ segments exactly — no per-hop +1. This
  composability is the durable fact: any per-cell shift register chains across
  cells with zero timing surprise.
- **Cost:** a mid cell fits 5 complex samples (2 inputs + 11 pinned state +
  17-instr program + auto-HALT = 31/32 words — the true dual-rail density
  ceiling; L=6 needs 35). The OUTPUT cell is capped at 4 so the INV-17 fan-out
  JUMP always has headroom (L=5 there would be 31+1 = exactly 32 — legal on
  paper, zero margin; not worth it for one sample). Net:
  `cells(D) = 1 (D≤4) else ceil((D-4)/5)+1` — depth 32 = 7 cells, 64 = 13.
- **I/Q skew is designed out, then gated anyway.** Both rails traverse the same
  cells in the same wavefront with identical per-rail structure, so a skew
  can't arise structurally — but the gate still includes a complex impulse
  (must land at index D on BOTH rails simultaneously), a quadrature tone
  (per-sample pairing preserved), and the single-rail ±1-skew mutations, which
  the exact pair-compare catches at the first misaligned index. For ANY future
  dual-rail block: gate the skew explicitly; a rail-swap/skew is invisible to
  single-rail comparisons and catastrophic downstream.
- **INV-33's no-data-words corollary bites exactly as documented:** this block
  has ZERO DataWords, so auto-allocated state would start at R0 and land ON the
  xi/xq inputs (the DelayBlock echo trap, complex edition). Every StateVar is
  explicitly pinned (di* at 2..L+1, dq* at L+2..2L+1, osave at 2L+2) and a
  dedicated test asserts every state register ≥ 2 at several depths.
- **No serialize-LOCK needed, and that's a checkable structural fact:** the
  chain is feed-forward LINEAR (each cell fed by exactly one predecessor — no
  feedback edge, no reconvergent fan-in of unequal-length arms), so INV-19/20
  do not apply; the COMPLEX_2IN2OUT saturated gate (depth 7, 2 cells) confirms
  pipelined == per-sample bit-exact with no lock.
- **Depth 13 is the boundary depth to gate:** it is past the real-rail
  DelayBlock's single-cell ceiling (12) — proof the chain goes where one cell
  cannot — and this block's own first 3-cell configuration. The FIR-style fold
  (FOLD_HEIGHT 4, even-column preference, partial last column accepted, ≤8
  across) handled 7- and 13-cell footprints with zero routing trouble in all 8
  D4 orientations.
- MAX_DEPTH=64 is a VERIFICATION boundary, not a geometric one (the fold could
  host more cells); the block raises above it rather than ship unverified
  depths. Deeper lines are SRAM-panel territory (INV-31) only if ever needed.

## ZeroCrossingRateBlock — windowed ZCR, single cell at exactly 32/32 words 2026-08-23

Windowed zero-crossing rate (tier 3, no GNU Radio streaming counterpart — golden =
an independent numpy reference from the pinned contract, the Crc16/Golay pattern).
Rate-REDUCING N:1: per non-overlapping `window_size` window, crossings/`window_size`
as ONE Q15 word, `count << (15 − log2 N)` (exact for the power-of-two-only param),
saturated to 0x7FFF at count==N. 34 tests, EXACT tol 0, first-build-after-fix clean.
Lessons:

- **The resolver reserves R31 as an auto-HALT, so a cell's REAL budget is data +
  state + instructions ≤ 31 words — and a pinned StateVar that lands in the
  instruction region fails SILENTLY.** `CellProgramResolver` packs instructions at
  `base_addr = 31 − instr_count`; it raises only when *data* collides
  (`base_addr < next_data_addr`), not when an explicitly-PINNED state register
  overlaps `[base_addr, 30]`. A 24-instruction first cut pinned `counter` at R7 ==
  base_addr: the state's initial 0 (= HALT) overwrote the first instruction at
  load and the block built + routed fine but emitted NOTHING (every trigger None —
  looks exactly like a routing/hop bug). Budget with the R31 reservation in mind
  and re-count after pinning (INV-33 sharpened: pin state, then CHECK
  `1 + data + state + instr ≤ 32` with the auto-HALT counted).
- **Two HALTs are free if the paths are ordered right:** main emit path falls
  THROUGH into `_skip:`'s HALT (label on a real instruction — the KeepOneInN /
  INV-13 branch-target rule), and the `_sat:` tail ends on the auto-HALT at R31
  (its WRITE/JUMP sit at ≤30, respecting the "no external op at R31" rule). That
  fall-through ordering is what brought 24 instructions down to 23 = fits.
- **Branchless sign-change detection:** `XOR prev, x` then `SHR R0, #15` — bit 15
  of the XOR is "sign bits differ", the logical shift makes it a 0/1 addend for a
  plain `ADD count, R0`. This also IS the tie convention (exact zero has sign bit
  0 = non-negative) — no compare/branch anywhere in the count path.
- **`count == N` must be caught BEFORE the shift:** `N << (15 − log2 N)` = 0x8000 =
  −1.0. One `CMP count, n; BR.Z _sat` pins the rate-1.0 window to 0x7FFF
  (1 − 2⁻¹⁵). The dual-emit (`{write}`/`{jump}` duplicated per path) is the
  standard INV-13 saturation shape and the build patches both fine.
- **Conventions must be PINNED, and the mutation stimulus must be chosen to
  EXPOSE the mutant (INV-4 sharpened):** the first "missing boundary carry"
  mutation (prev reset to 0 per window) COINCIDED with the true golden on the
  obvious stimulus `[+]*N + [−]*N` — the phantom (0→−) crossing equals the real
  boundary (+→−) crossing, `[0, 8192] == [0, 8192]`, caught only because the
  mutation test itself failed. There are TWO distinct no-carry mutants
  (reset-prev-to-zero vs skip-the-boundary-pair) and each needs its own stimulus
  (window 1 ending − with window 2 all −, resp. window 1 ending + with window 2
  all −). Also pinned: implicit-zero predecessor before the stream (constant
  NEGATIVE input reads 1/N in the FIRST window only — documented, tested), the
  exact-zero tie (window `[+, 0, 0, −]` = 1/4, mutant zero-as-negative = 3/4),
  and the off-by-one window mutant (closes one sample early → wrong phase AND
  wrong words).
- The float `process_reference` must quantize the input to Q15 BEFORE taking
  signs (a tiny negative float that rounds to 0 is NON-negative on-chip).

## FLLBandEdgeBlock re-fold: perimeter RING → compact serpentine (no dead interior) 2026-08-17

A design-quality re-fold, DSP untouched: the FLL's `default_layout` was a
perimeter RING (8×5 at fs=17, 8×8 at the fs=27 ceiling) whose enclosed
interior (6×3 = 18 cells at fs=17) was walled off from every other block — the
robust_rx complaint. Re-laid as a compact serpentine: head row of the 7
fixed cells (phase…fanout, W→E, fanout turns SOUTH) + the 2·n+2 chain cells
(ci*/cq*/berr/pi) snaking through three boustrophedon column PAIRS
((6,5),(4,3),(2,1), balanced depths summing to n+1), so `pi` lands at (1,1)
for n≥2 and one transit at (0,1) closes the loop NORTH into phase's SOUTH
face. Folds: fs=3 → 7×2 (3 transits, fb @4), fs=5 → 7×2, fs=8/11/14 → 7×3,
fs=17 → 7×4 (was 8×5), fs=27 → 7×5 (was 8×8) — all ≤7 both dims (the AGCCC
≤7-wide routability rule), every in-bbox hole on an OPEN edge (verified by
flood fill), fb corridor @2 for n≥2. All gates unchanged-green: 19 FLL tests
(bit-exact sweep incl. fs=3/27, saturated==per-sample, acquisition
0.05/0.10 → ~1e-4, chain BER 0 @0.18), orientation 8/8, legality, binding,
route ratchet; robust_rx regenerated through the real import→auto_pnr→save
path (BER 0 / ctl 0.173, userpath + real-GR-client duplex gates green).
Constraints that BIT during design (all pre-empted, first build worked):

- **The serial trigger chain must remain ONE connected fwd-face path** —
  the ring's only load-bearing property. Long handoffs (phase→rotate @5,
  fanout→cq0 @n+1, ci-tail→berr @n+1) ride it as HOP<31 transit words; a
  serpentine has this property exactly when every consecutive layout slot
  is face-abutted, which forces the pair-transition geometry (a pair's two
  columns share their BOTTOM row; pair-to-pair hand-off along row 1).
- **The chain count 2n+2 is always EVEN, so three column pairs partition it
  exactly** (a+b+c = n+1) — no padding, no partial-parity trap (INV-14's
  even-column rule shows up here as "pairs", which sidesteps it entirely).
- **The loop-closure geometry is pinned by lock_face=SOUTH (reset
  default):** pi (or the transit run for n=1) must deliver onto phase's
  SOUTH face, i.e. the corridor must END at (0,1) emitting NORTH — same
  closure as the ring/Costas 4×2, so the phase/pi programs are UNTOUCHED.
  `_apply_internal_feedback` traces pi's own fwd_face (WEST along row 1,
  one NORTH turn) and co-patches the dphase WRITE + lock-clear WRITE.CFG to
  the same hop — the QAM16 "pi mid-array with divergent hops" trap does not
  bite because pi's layout face IS the feedback direction and pi's trig is
  `__terminate__`.
- **fanout's face words need NO re-authoring:** `_apply_rotate_tap_face`
  sets `face_internal` from the PLACED layout face (now SOUTH, was EAST)
  and `face_tap` from the route's first-hop exit — authored values are
  placeholders. Only the layout moved.
- **INV-4 on a layout change:** proved the gate SEES the fold by mutating
  pi's face EAST — the loop never closes, egress 1/80 (a cached/veneer gate
  would have stayed green).
- **Example-level honesty:** the regenerated robust_rx placement spreads
  the blocks more (81/120 cells vs 58 — extra ROUTE corridor cells, not
  block cells) yet its pinned route-quality TOTAL excess is unchanged at 4
  (now the two Costas→slicer tap corridors rounding their own 4×2 folds;
  comment re-pinned). Per the Gardner-refold warning, the fold's effect on
  the DENSE example was verified end-to-end, not assumed from the block.
- **A regression test that USES a block as its hazard generator breaks when
  the block improves:** `test_port_transit_guard.py` built its pinch from
  the live FLL at (1,1) — the 7-wide fold no longer pinches, so `assert not
  rep.ok` failed. The guard is a ROUTER property; the fix is to pin the
  HISTORICAL layout in the test (a `legacy_fll_ring` fixture monkeypatching
  the old ring `default_layout` verbatim), not to keep the block wide. The
  named-reason assertion ("port-transit hazard") keeps the pin non-vacuous.
- Generalization note (NOT done here): AGCCC's 7×5 perimeter ring has the
  same dead-interior shape (interior 5×3 = 15 cells); its CORDIC chain is
  one serial run too, so the same head-row + column-pairs scheme should
  apply, but its `upd`→`hold` @1 abutment and mid-block dual-face `tap`
  differ enough that it needs its own verified pass.

---

## Continuous cross-chip route highlight (multi-chip GUI) 2026-08-17

Selecting a block now lights its WHOLE board-level stream path — sibling
connections on other chips, the inter-chip wires it crosses, and overlay
polylines for the segments that have no design-level route (transparent-wire
transit corridors + far-die deliveries). Closes the 2P2S "broken fly line"
report.

- **The seed matcher silently excluded cross-chip connections:**
  `connections_terminating_at_cell` hard-skipped any connection whose
  `route_chip_of` differed from the selected chip — a far-die input net
  (source port on chip A, target block on chip B) never seeded, so the
  far-die block's incoming net stayed dark. Fix: route-ENDPOINT matching
  stays gated on the route's own chip, but the block-endpoint resolution
  (which carries its own per-endpoint chip check) now runs for every
  connection.
- **The synthesized segments are the physical truth, cheaply derived:** a
  transit chip's corridor is in-port cell → out-port cell (the `_in` →
  `_out` twin, the transparent-wire convention — exactly the bus crossings
  the composite hop arithmetic counts), and the far-die delivery is
  in-port cell → the block's resolved input cell. Drawn as overlay path
  items in the related-highlight color (L-bend when unaligned), rebuilt on
  every selection change, cleared on deselect.
- **Expansion walk** (`_cross_chip_expansion`): BFS over the seeded names —
  a chip-spanning connection contributes per-chip transit + delivery
  segments and its wires; a connection ending at a wired OUTPUT port pulls
  the wire plus either the continuing connection on the next chip or the
  synthesized egress transit to the board output; a connection starting at
  a wired INPUT port pulls the wire + its upstream feeders. Bounded guards
  throughout (no infinite wire loops).
- `InterChipWireItem` gained the same related state ConnectionItem has; the
  canvas now tracks its wire items across re-renders.
- Gate: `placekyt/tests/test_crosschip_highlight.py` on the SHIPPED
  gain_2p2s.kyt via the real MainWindow open path — far-die input path
  (conns + wire + overlays), near-die egress transit, pair isolation
  (selecting pair-2 never lights pair-1 wires), and deselect-clears.

---

## robust_rx + complex_math examples, audio_meter true-RMS row — the two-complex-stream CLIENT contract closed 2026-08-16

Three integrations in one pass: `examples/robust_rx/` (FLL→Costas→slicer BER 0
at foff=0.18 with the Costas-only chain as the ON-CHIP negative control, plus
the shipped coherent_bpsk_rx.kyt failing the same burst), `examples/
complex_math/` (AddCC/SubCC/MultiplyCC on two Q15-snapped analytic tones,
bit-exact vs the blocks' references, mixer bin 10+17=27/256 asserted with
INV-4 fakes), and a third `rms` stream (RMSBlock, alpha 0.0625) in
audio_meter. All three: .grc→import→auto_pnr→.kyt, headless demo, §5b gate,
userpath gate, real-GR-client gate.

- **The AddCC-family GR-client path was NEVER exercised before — three real
  engine gaps fell out of the first live drive** (the block gates used the
  bespoke complex2 harness, which hand-addresses registers):
  1. **Broker port-complex groups were sized to ALL input regs.** For a
     4-register two-pair block the broker relayed a 4-operand group per net,
     but each stream injects only ITS pair → the relay protocol broke and
     the join fired on half-primed garbage. Fix: `bus_router` slices the
     group to the target port's (I,Q) pair (and keys `done`/`grp` by the reg
     group so BOTH pairs may divert at one broker); `build`'s broker landing
     sizes `data_addrs` to the pair.
  2. **stream_targets handed every stream the block's FULL input register
     list** — stream b's packet landed on ai/aq and clobbered stream a. Fix:
     `_target_port_pair_idx` slices the landing's positional `data_addrs` to
     the net's own pair (only for >2-input-reg blocks; single-pair blocks
     byte-identical).
  3. **out_tag ownership was nondeterministic**: both ingress streams of one
     block walk forward to the same egress net, both claimed its tag, and
     the duplex demux hands words to the FIRST claimant in client THREAD
     order. Fix: deterministic ownership — the first ingress stream in
     project-connection order (the .grc's first-input wire) owns the tag,
     partners resolve None. CONTRACT: name the sink after the block's
     FIRST input's stream.
  The pair slicing is GATED on ``src_complex is True`` (the importer's
  complex-source marker): the verification harness wires all FOUR rails as
  separate nets (src_complex None) and expects the legacy single 4-operand
  broker group — the ungated first cut broke ~100 add/sub/multiply harness
  tests (regression sweeps are not optional). And ``src_complex`` was NOT
  serialized to .kyt — the shipped artifact silently lost the contract on
  load (shipped-kyt gate caught it); it now round-trips in project_io.
- **Duplex complex egress didn't exist either**: `_process_batch_duplex`
  matched exact out_tag only, so a complex chain's yq rail (tag+1) parked
  forever. `complex_out` now rides stream_targets into the duplex demux
  (tag, tag+1 collected in emit order = interleaved I/Q) — and complex_out
  detection needed the PROJECT-NET check (the AddCC family declares ONE
  interface output register, the INV-17 packet emitter, yet the importer
  synthesizes the real yq→port net; spec-only detection said False).
- **A complex ingress stream cannot fan out on-chip**: the importer
  auto-splices a StreamSplitterBlock for a 3-arm source fan-out, but the
  relay is SINGLE-RAIL (port x) — the Q rail has nowhere to land (and the
  splitter layout didn't route anyway). Pattern: one source pair per block
  (fec_link's duplicated-ingress), six streams.
- **Output-word convention traps, both directions**: a complex-input chain's
  sink emits RAW word floats (auto mode — the receiver convention), so
  robust_rx's bit scopes need NO ×32768 (a rescale block shows ±32768) and
  the client reads raw ints; complex_math's outputs are Q15 VALUES, so its
  sources must set `output_words: q15` (else the LMS "missing constellation"
  ±30000 display). Check which convention a chain is in BEFORE wiring scopes.
- **GR python blocks in a loop segfault**: building a client flowgraph in a
  loop drops each iteration's Python references; unreferenced py blocks
  crash the C++ scheduler at start (rc=-11, no Python frame). Keep a
  `self.keep` list.
- **audio_meter 3-stream layout needed a 3-cell deterministic nudge**: the
  compact packer seats the meter head (Abs) on the port row and NO rms
  placement routes (the port fan-out's third corridor is walled; the full
  9-attempt auto_pnr sweep exhausts). Exhaustive scan over (Abs, rms 2×2,
  squelch) seats found ONE whole-DRC-clean routable class: Abs into the
  DC-blocker pocket (4,3) (+6 excess on its one corridor, pinned in the
  ratchet), rms in the free lower half, squelch off the x16_out column
  (its (9,2) seat = the single-cell in==out face hazard). The abs-at-(1,0)
  seat routes with excess 2 but violates the port-fanout keep-off — DRC
  said no, so we didn't ship it.
- robust_rx reused the FLL chain gate's topology/params/stimulus VERBATIM
  (RC-shaped burst — no MF needed, the symbol instants are ISI-free; the
  slicer replaces the host-side sign decision at 2 sps with a host-side
  phase pick). auto_pnr placed the 22-cell ring + 2 Costas + 2 slicers
  clean on the first try (58/120, no port-cell transit — the demo asserts
  that hazard check non-vacuously; note `Connection.route` is a
  `list[RoutePoint]`, not an object with `.points`).
- audio_meter's RMS row: alpha 0.0625 = 2048/32768 EXACT in Q15 ⇒ chip and
  GR run the identical time constant and the whole trajectory compares
  (no warm-up term); bound = the block report's 16 LSB above its 0.18-FS
  floor, measured 3.
## ROUTER+BUILD hardening — used-port-cell transit is a NAMED failure; the locked NCO/FM block-consumer rails un-shifted 2026-08-16

Two engine-level closures, one campaign (no block datapath changed):

**A. The FLL port-cell transit hazard is CLOSED (bus_drc check (d), "port_transit").**
Reproduced the FLL lessons pinch verbatim (8-wide ring at anchor (1,1), consumer
south): the shipping path was the MAZE ESCALATION in `_run_router` — `route_all_bus`
already refused via the INV-32 own-broker guard, but the controller then escalated to
`route_all_maze`, which had NO chip-port awareness at all and returned
`(7,1)…(1,0),(0,0),(0,1)…` rep.ok=True (the CP-SAT router, by contrast, always had a
hard foreign-port constraint). The bus router's own +1000 soft penalty was also
shippable when pinched (no alternative path ⇒ pays the penalty silently). Closure,
defense-in-depth: (1) `check_port_transits` in `engine/bus_drc.py` — occupation of a
USED port cell (a port that is an ENDPOINT of any connection; usage from the LOGICAL
nets, since input injections may carry no waypoints) by a net that doesn't own it is a
named violation; folded into `check_bus` (so `_drc_gate` demotes) AND surfaced as a
hard `port_transit` error at project DRC/build (hand-laid routes); (2) hard walls in
all four routers (bus `hard_forbid`, maze obstacle set + broker candidates, heuristic
occ, CP-SAT already had it) so a detour is TAKEN when one exists; (3) a
`_demote_port_transits` backstop on `_run_router`'s final report, whatever router made
it; (4) the bus router names the hazard ("port-transit hazard, bus_drc check (d)") via
a relaxed diagnostic re-probe when the wall is what made a net unroutable. UNUSED port
cells stay plain routing cells (soft-penalized only) — `test_different_sink_share` and
the column-9 passage still route. KEY SCOPING TRAP: a block placed ON a port cell
(direct-injection idiom, `place_block(...,0,0)`) makes the port cell that net's OWN
terminal — ownership must include the net's source/target BLOCK cells or the guard
false-positives half the router suite. Gates: `placekyt/tests/test_port_transit_guard.py`
(INV-4 proven: pre-fix the pinch ships rep.ok=True through (0,0)).

**B. The INV-20 "auto-placed lock corridor" known limit: the corridor was NEVER the
problem — the tail-WRITE classification was.** Probing locked FM chains across
placements/orientations/auto_pnr showed the `transit_unlock` adjacency + the
`_apply_internal_feedback`-derived unlock hop are RIGID under every transform (the old
adjacency-loss sightings were the 2026-07-22 re-fold SET-dedup self-overlap bugs,
already fixed). The REAL, placement-independent residual: a locked block feeding a
DOWNSTREAM BLOCK shipped SHIFTED rails — the consumer read (yq, 0) — in EVERY
placement, on THREE build paths that all mis-handled the emit cell's lock-clear
`WRITE.CFG` (which sits AFTER the yi/yq rails):
  1. `_patch_complex_packet_last_handoff` (routed 2-net packet) counted the WRITE.CFG
     as one of the "last N WRITEs" → CFG steered down the data corridor, yi stranded
     @1. Fix: skip config WRITEs in the tail selection (the skip
     `_patch_last_write_handoff`/`_patch_complex_output_port_handoff` already had).
  2. The single-net complex + carries-handoffs branch patched ONLY the last data
     WRITE → only yq delivered. Fix: patch the last `len(output_registers)` data
     WRITEs to consecutive burst regs (the packet-last form).
  3. The ABUTTED pair (what auto_pnr's compact pack produces) ran the single
     `_patch_last_write_handoff` once per rail net — LAST-WINS into one WRITE, both
     rails → target R0. Fix: `_patch_complex_abutment_tail_handoff`, called ONCE per
     source cell, steering the last N data WRITEs to the target's own input regs.
DEBUGGING METHOD that found #3 after #1 looked fixed: dump the BUILT emit cell
(`bres.chips[0].cells[(x,y)]` + `disassemble_word`) — two `WRITE @1, 0` side by side
is unarguable. GATE: `verification/tests/test_locked_chain_autopnr.py` — locked FM
chain saturated bit-exact vs composed references across 3 placements + full auto_pnr,
with a BLOCK consumer (the INV-4 pin: pre-fix (4320, 0) vs ref (24184, 4320)) and a
locked-NCO consumer case. The fsk4 modem never saw this because its FM feeds the PORT
(the 2026-07-22 egress fix); every locked-block→block chain was silently broken.

---

## fec_link example — burst→interleave→correct→CRC-verdict; one out_tag per stream; layout-dependent saturation 2026-08-16

The FEC protocol-link example (`examples/fec_link/`): three streams on one
chip — 'tx' bytes→Unpack(8)→HammingEncoder(4:7)→BlockInterleaver(4×3),
'txcrc' the same bytes→Crc16(frame_len=12), 'rx' burst-corrupted channel
bits→deinterleaver→HammingDecoder→Pack(8). A 2-bit channel burst is dispersed
into two codewords, corrected, and the chip TX CRC word matches the CRC
recomputed over the recovered bytes; the on-chip no-interleaver control fails
both. 9-test gate (incl. the real-GR-client user path on 58950) all green;
first placed run was already bit-exact end to end. Durable lessons:

- **The GR client contract is ONE tagged egress per source stream** —
  `port_config.stream_targets` resolves a single `out_tag` per `stream_id`
  (BFS to the FIRST output net) and `sim_bridge` buckets only that tag per
  stream; a second egress of the same stream would park unclaimed in
  `_tag_buf` forever. So the dispatched design's "stream 'tx' port fan-out,
  2 arms with dual tagged egress" is NOT expressible on the client path: the
  shipped form is the cordic_polar pattern — the SAME message bytes ride two
  streams ('tx' and 'txcrc'), the chip port still fans out 3 arms through
  the INV-24 broker machinery, and every arm's egress has its own claimable
  tag. Deviation, mechanism, and code sites documented here on purpose.
- **Interleaver framing arithmetic must include the PIPELINE ZEROS.** The
  streaming BlockInterleaver emits N zeros per stage (group delay), so two
  stages put 2N=24 zeros ahead of the coded stream — 24 mod 7 ≠ 0 misframes
  EVERY Hamming codeword downstream. Fix at the host channel: prepend
  12·k−2N zeros so the total prefix is whole codewords AND whole decoded
  bytes (here 60 → prefix 84 = 12 zero codewords = 6 zero bytes). Also pad
  the TX message (6 zero bytes = a dropped partial CRC frame) so the coded
  stream stays block-aligned and flushes both stages. Burst spacing:
  consecutive interleaved bits deinterleave exactly cols(=3) apart, and
  o, o+3 straddle a codeword boundary iff o mod 7 ∈ {4,5,6} — the first hit
  is then always a parity bit, the second always a data bit. All derived +
  asserted at stim import time, and the stim mirrors are gate-checked
  against the blocks' own `process_reference`.
- **Chain-level saturation is LAYOUT-dependent — the route-quality ratchet's
  hazard is real and measurable.** On the compact packer's wirelength-
  optimal layout the port→crc corridor circumnavigated the array (+14 over
  manhattan; per-net ratchet cap is +8), and under saturated whole-burst
  drive the 1:14 rate-EXPANDING tx chain deadlocked at 1/252 words (the
  2026-07-27 expanding-chain class). On the shortest-path layout (CRC cell
  nudged beside the input port; total excess 0) the SAME merged three-stream
  saturated drive is EXACT. The packer's objective doesn't see routed
  corridor length, so `build_kyt.py` runs a deterministic post-P&R
  refinement (move the 1-cell CRC, re-route, accept only clean DRC + excess
  ≤4, verified end-to-end downstream); the .grc still ships per-sample
  paced for import-path robustness. Both sides pinned in the gate.
- **Importer coercion traps for FEC params:** an expression param
  (`stim.crc_frame_len()`) is SILENTLY dropped for the block default (the
  sps=256 class) — ship literals and pin them in the gate
  (`test_import_pnr_build_ok` asserts the placed Crc16 got
  frame_len/poly/init). Hex literals (`0x1021`) now coerce (`int(s, 0)`
  fallback in `_coerce_params`), and `blocks_short_to_float` /
  `blocks_float_to_short` joined `_PASSTHRU_IDS` so the crc16(short)→sink
  display cast splices like the byte casts (pinned by the same test).
- **REAL IMPORTER BUG the control variant flushed out — a hash-derived
  stream tag could COLLIDE with the fixed 'tx'/'rx' tags, per-process
  nondeterministically.** `_stream_tag('txcrc')` hash-derives to 10 == the
  FIXED 'tx' tag; the fixed path returned 10 unconditionally (no probing),
  so if the DERIVED id was assigned first both sinks' egress nets carried
  tag 10 and both GR sinks demuxed ONE stream (the CRC word appeared inside
  the tx bit stream). Assignment order = connection-list order, and
  `_splice_converters` iterated a SET — so the winner flipped with
  PYTHONHASHSEED: the full-suite process failed while 8 isolated repro runs
  passed, and 2-of-4 parallel processes reproduced it. The shipped .grc was
  safe only by edge-order luck (its tx sink edge is direct, so 'tx' always
  registered first). FIX: derived tags now probe past `set(_STREAM_TAGS.
  values()) | used`, and converter splicing iterates `sorted(conv_names)`
  (imports are process-deterministic). Pins:
  `test_stream_tag_never_collides_with_fixed_tags` + the control test's
  distinct-tags assert. META: when a gate fails ONLY under the full suite,
  suspect PYTHONHASHSEED/set-order before load or layout lottery — run N
  PARALLEL single-repro PROCESSES (not N trials in one process, which share
  one seed) to separate the two.
## Latent-defect sweep: AGC bare-MULQ stall, MagSquared wrap corner, Costas dphase landing — three flagged hazards closed 2026-08-16

Maintenance trio — every mechanism was discovered and root-caused by a recent
factory build (credit: the RMSBlock+RMSCFBlock build flagged the first two, the
AGCCCBlock build the third); this pass ported the proven fixes to the older
blocks that carried the same latent bugs, each with an INV-4 regression proven
to FAIL on the pre-fix block.

- **AGCBlock (agc_ff): the bare-MULQ IIR stall at the GR DEFAULT rate=1e-4 —
  FIXED with a cheaper exact accumulator than the RMS form.**
  ``gain += MULQ(rate, err)`` zeroes every increment with
  ``|err| < 2^15/rate_q`` (10923 LSB at rate_q=3); direction-asymmetric, so
  only a RISING gain froze (start gain below the settled point to pin it — a
  falling AGC self-repairs and false-passes). Fix = full-precision error
  feedback, but NOT the RMS masked 15-bit form: the single 32-word cell can't
  afford its mask word + hi/t scratch. The ADC IDIOM IS CHEAPER AND EXACT:
  track ``S = gain<<16 + acc`` and step by ``prod<<1`` —
  ``MULQ`` (hi) + ``ADD gain``; ``MUL`` (lo16) + ``SHL #1`` + ``ADD acc``
  (C = carry) + ``ADC gain, zero``. The identity
  ``prod<<1 == (prod>>15)<<16 + ((lo16<<1)&0xFFFF)`` is exact under MULQ's
  floor, needs NO mask word, one scratch state, and a 16-bit residual.
  Register reclaims that made it fit EXACTLY (1 input + 4 data + 3 state + 23
  instr = 31): emit the output RIGHT AFTER the MULQ (WRITE preserves R0 — the
  Costas ``{write}``-first idiom), freeing ``out_save``/``abs_save``; re-derive
  the sign flags after the WRITE with ``OR R0, R0`` (don't rely on WRITE
  preserving flags). Also fixed the reference model: it claimed MULQ
  round-to-nearest — MULQ is FLOOR (the 80-LSB GR-loop gates hid the lie; the
  new chip gate is bit-exact and would have caught it). Regression: 600
  samples, rate=1e-4, ref=0.5, gain=0.4 rising — pre-fix moved 0 LSB (total
  freeze), post-fix climbs bit-exactly vs the model; plus a 130k-sample model
  convergence run (the AGCCC regime-mirroring shape). Watch the harness
  quantizer: ``_fq`` scales by 32767 but the block by 32768 — feed the
  reference the CHIP's words (s16/32768) or a 1-LSB input skew masquerades as
  a datapath mismatch.
- **ComplexToMagSquaredBlock: the re=im=-1.0 wrap corner — FIXED with the
  RMSCF per-step guard.** ``0x8000 + 0x8000`` wraps the accumulator to ZERO
  with N CLEAR, so the single end-check ``BR.N`` emitted 0 for a full-scale
  input. Per-step guard (``MULQ re,re; BR.N; MACQ im,im; BR.N``) — the wrap
  requires the FIRST product to already be 0x8000, so checking after each step
  closes it. The existing bit-exact reference already modelled saturation
  correctly (unbounded ints + min-clamp) — only the chip wrapped; the derived
  tolerance and all 21 existing gates unchanged. Corner regression pinned
  bit-exact (pre-fix emits 0).
- **ComplexCostasLoopBlock: the ``dphase`` feedback landing was an input Port
  — converted to a pinned STATE register (the AGCCC ``ginc`` recipe), and the
  brokered-corridor hazard is now REPRODUCIBLE in a gate.** ``resolved_io``
  counts input-role registers as host operands; under a DIVERTED input
  corridor (two x16_in nets sharing a corridor cell — the divert mechanism in
  ``_resolve_input_landings``) the broker deliver entry relays one delivery
  per resolved input reg, so a stale third burst word landed in R2 every
  sample and ran the loop OPEN. The 4x2 fold never gets its input brokered in
  the single-block gates (probed: all 8 D4 orientations x 5 anchors ride
  straight) — the hazard needs a SECOND port net: GainBlock@(3,0) pins the
  row-0 corridor and the Costas@(1,1) ``in_xi`` net diverts at (1,0) → broker
  landing (burst regs + deliver entry). Regression drives that landing
  contract and asserts bit-exact vs the closed-loop reference: pre-fix
  194/200 samples diverge, post-fix 0. The state-var change is byte-identical
  on the identity build (dphase pinned at the same R2; state names resolve
  before input names in ``_resolve_named_input`` AND in
  ``_apply_internal_feedback``'s input-then-state fallback — both paths land
  on R2); the ONLY bitstream delta is the dormant input broker shrinking from
  3 relays to 2 (the fix itself). ``reset_per_batch=True`` on the landing (a
  fresh packet cold-starts with no pending update, like phase/freq).
  CoherentRXBlock reuses the phase cell verbatim and inherits the fix. NOTE:
  QAM16ComplexCostasLoopBlock still carries the dphase-as-input pattern
  (same latency conditions); port the same recipe when it is next touched.
- All blast-radius gates green with no tolerance or gate adjustment:
  agc (13) / agc_cc (37) / complex_mag (22) / complex_harness /
  costas loop+build (58) / orientation+saturation (308) / QPSK-modem BER +
  coherent-RX (22) / audio_meter.
## FLLBandEdgeBlock — the composite-loop ceiling holds a 21-cell RING: coarse-FLL bit-exact, chain-proven 2026-08-16

GR `digital.fll_band_edge_cc` (samps_per_sym, rolloff, filter_size, bandwidth
VERBATIM). The hardest block of this wave — NCO + complex rotate + FOUR real
band-edge correlators + squared-magnitude error + 2nd-order loop, all inside
ONE feedback ring — landed FIRST-TRY bit-exact on chip (the settle-architecture-
first discipline did its job: every hard decision was settled in Python before
any silicon). 19 tests green (`test_fll_band_edge.py`) + orientation (8/8) +
legality + bespoke saturated gate. End-to-end: FLL→Costas placed+routed on ONE
chip recovers BER 0 at foff=0.18 cyc/sample where the chip Costas-only chain
fails at BER 0.17 (the negative control), matching GR's competence at GR's own
operating point (0.05 — see the loop-strength note below).

- **PIN GR LIVE FIRST paid for itself twice.** (1) GR 3.10's `design_filter`
  normalizes by `sum(tap^2)` (POWER), not `sum(tap)` — older sources differ.
  (2) GR's out/freq/error streams lag the input by **filter_size samples** —
  `set_history(filter_size+1)` zero-pads the stream head, and `work()` indexes
  `in[i]` from the padded start. Compensate the model input by exactly fs
  zeros and the float model matches GR's own freq/error OUTPUT STREAMS to
  float32 rounding (max |Δfreq| 2.5e-7 over 3000 samples) — the strongest
  possible structure pin, far stronger than tap comparison alone (print_taps
  only prints 4 significant digits). The chip has NO such delay (INV-2: state
  it, don't lag-search).
- **The band-edge ALGEBRA collapses 2 complex FIRs to 4 real dot products.**
  `taps_upper = conj(taps_lower)` elementwise, so with a=Re, b=Im of
  taps_lower and A=Σa·y, B=Σb·y (complex): `err = |L|²−|U|² = 4(Ar·Bq−Aq·Br)`.
  Two systolic chains (yi-history, yq-history), each cell holding ONE delay
  segment + BOTH tap sets (3 taps/cell: 6 data + 4 state + 17 instr = exact
  fit; the complex-FIR budget mirror-image). The ×4 and the radians→phase-word
  unit map fold into the STORED gains: `ah = 4α/π`, `bh = 4β/π` (< 1 for
  bandwidth ≲ 0.55; raise beyond — a documented Q15 HW-deviation).
- **The RING fold is the general shape for a big loop.** The whole sample pass
  is ONE serial trigger chain around a W×H rectangle perimeter (interior
  empty), pi lands next to phase, and the leftover perimeter slots ARE the
  feedback transits — Costas's 4×2 fold, scaled to 22+ cells. Every internal
  handoff (including fanout→cq0 crossing the whole I-chain, and I-tail→berr
  crossing the Q-chain) rides the single fwd-face corridor as HOP<31 transit
  words. Fully-serial execution ⇒ no INV-20 fan-in race by construction; the
  INV-19 phase-LOCK + pi WRITE.CFG (Costas idiom verbatim, lock_face=SOUTH
  reset-default preserved by the ring's return geometry) covers saturation.
- **The freq integrator NEEDS the RMS error-feedback idiom.** Band-edge error
  is small (power-normalized taps ⇒ |err| ~0.01–0.12), so `MULQ(bh, err)`
  truncates to ZERO below |err·bh| < 2^15 and the integrator stalls ~0.006
  cyc/sample short — the RMS-stall lesson, predicted and pre-empted:
  `bh·err = (MULQ<<15) + (MUL&0x7FFF)` exactly, fraction accumulates in a
  15-bit facc. Mutation `test_mut_dropped_error_feedback_accumulator` proves
  the gate sees it. (Rescaling err up + gains down does NOT help — the
  truncation floor is a property of the PRODUCT, not the operand split.)
- **A dual-face "fanout" cell decouples the tap from the compute.** rotate
  (the proven Costas order-4 cell, sinv=+sin for the FLL's exp(+jφ)) stays a
  pure internal cell; a cheap 14-instr fanout cell forwards yi/yq into the
  chains AND taps the corrected pair out (last-two-WRITEs + tap_trig = the
  qpd 2-rail egress idiom). Trying to fuse the tap into rotate overflows 32
  words — dedicate the head, as the dispatch warned.
- **Saturated ≠ livelocked: the serialized ring costs ~2500 sim events/sample
  (LINEAR in pipeline depth — measured 778/3253/5731/10686 for N=1/2/3/5).**
  `run_block_dut_pipelined`'s default 2000/sample cap reports a false
  "livelock"; the block is NEEDS_BESPOKE with its own saturated gate at
  4000/sample, bit-for-bit equal to per-sample at N=100.
- **ROUTER HAZARD (CLOSED 2026-08-16 — now a NAMED failure, see the
  ROUTER+BUILD hardening entry / INV-32 port-input hardening): a corridor may
  transit the x16_in PORT CELL.** An 8-wide block ring pinches both side
  channels against the corner chip ports; the router (the MAZE escalation —
  route_all_bus itself refused) wrapped the fanout→Costas corridor THROUGH
  (0,0) x16_in + its delivery broker (0,1) — route "ok", build "ok", chip
  dead in 6 events (injections swallowed). Such a route is now impossible to
  ship (hard walls + `port_transit` DRC); the pinched geometry fails NAMED.
  The chain test's placement (consumer NORTH of the ring with 2 free rows)
  remains the correct ROUTABLE layout — the pinch is genuinely unroutable.
  Also: `add_logical_connection` AUTO-SYNTHESISES the
  yq-sibling of a complex block→block link — wiring BOTH rails by hand
  double-delivers (the importer's dedupe only sees its own nets).
- **Loop-strength honesty: the Q15 k=1 Costas is a ~π× stronger loop than GR's
  float costas at the same loop_bw** (the Costas block's documented direct-Q15
  gain mapping). Chip Costas-only pulls ~0.12 cyc/sample clean where GR's
  breaks at ~0.03 — so the chain's negative control lives at 0.18 on-chip vs
  0.05 for GR. Assert each golden at ITS OWN operating point (INV-26), and
  remember GR's chain needs ~450 settle symbols at FLL bw=0.1 before its
  Costas sees a settled residual (a too-short BER skip reads a locked GR
  chain as BER 0.07 — measured).
## RationalResamplerBlock — the interp+decim combo ships as a POLYPHASE cell; GR's D-offset alignment is real and load-bearing 2026-08-16

GR `filter.rational_resampler_fff` (GRC **Rational Resampler**), tier 3. The
manifest's substrate-reality-check dispatch resolved to outcome (a): the
decim>1 & interp>1 combo genuinely fits one cell for a small (L, M) range —
but only in POLYPHASE form, not the zero-stuff L-burst the dispatch sketched.
50 tests green vs LIVE GR on the first build attempt (the budget probe grid
was the "attempt" that set the caps). Durable lessons:

- **Polyphase, not zero-stuff, is what makes the combo fit.** The FIR interp
  path's unrolled L-burst runs the FULL N-tap MAC L times per input (N·L
  MACs) over an N-deep stuffed delay line. The polyphase decomposition
  (`y_full[nL+p] = Σ_m h[p+mL]·x[n-m]`) runs N MACs TOTAL per input over a
  `K = ceil(N/L)`-deep INPUT-rate delay line shifted ONCE. Same arithmetic,
  bit-identical (the skipped terms are exact zeros; a wrapping add of 0 is
  the identity — so the inherited zero-stuff single-cell Q15 reference
  predicts the polyphase datapath exactly, keeping MAC order oldest-first =
  descending tap index within each arm). It even beats the pure-interp
  zero-stuff cap at L=3 (3 taps vs 2).
- **Measured single-cell budget (probed against the real resolver; program =
  K + N + 6L + 1 words, data = N+2, state = K+1):** L=1 → 5 taps, L=2 → 4,
  L=3 → 3, **L≥4 fits NOTHING** (the L·(gate 4 + emit 2) fixed cost alone is
  ≥26 words). Any M in [1, 32767] (M only changes a data word). The probe
  grid's failures came back as clean `No register space for state 'dN'` /
  `Not enough register space: NN instructions` build errors — the resolver
  allocator IS the budget oracle; the hand arithmetic matched it exactly.
- **A 4-word countdown mod-M gate:** `SUB c,one; MOVE c,R0; BR.NZ skip;
  MOVE c,decim(reload)` — 2 words cheaper than the up-counting CMP/XOR gate
  the FIR decim path uses, because ALU flags SURVIVE `MOVE` (INV-34) so
  `BR.NZ` reads the `SUB`'s Z through the store-back, and reload-by-MOVE
  replaces XOR-clear + CMP. Countdown-seeded at D+1 it also encodes the
  alignment offset for free. Branch targets label the next arm's SUB or the
  final HALT (real instructions, never a `{write}`/`{jump}` placeholder —
  the INV-13 miscompile). Emit paths FALL THROUGH the skip label into the
  next arm (a remote JUMP does not halt the issuer).
- **GR's output alignment is NOT phase 0 — pin semantics with impulse probes
  before assuming.** Live GR emits `y_full[D::M]` with `D = L*(ceil(N/L)-1)`
  (its polyphase arms span x[i..i+K-1] FORWARD). Consequence worth
  remembering: `rational_resampler_fff(1, M, taps)` is NOT sample-aligned
  with `fir_filter_fff(M, taps)`, and (1,1,taps) is the plain FIR ADVANCED
  by N-1 samples. A phase-0 port would have passed a lag-searching gate;
  the suite pins the alignment with an impulse test + a phase-0 MUTATION
  that must fail. Also: GR truncates the TAIL (scheduler forecast, deficit
  ≤2 observed) — the chip's deterministic count is `ceil((n*L - D)/M)`;
  gate the DUT count by formula and GR as a prefix, never `len(gr)`.
- **Auto-design is float32-parameter-faithful firdes:** GR gcd-reduces
  (L, M) FIRST (getters return the reduced values; user taps are NEVER
  reduced — only an info log), computes rate/trans_width/mid in FLOAT32
  (a float64 replica lands 1 tap off at e.g. (2,3) and (5,3) — the ntaps
  rounding crosses a boundary), designs `firdes.low_pass(gain=L', Fs=L',
  KAISER beta=7)`, and `taps()` zero-pads to a multiple of L'. Replicated
  via the repo `_firdes` + `np.float32` params: float-bit-exact live, gated
  Q15-EXACT (INV-16). The design (≥17 taps, gain L') NEVER fits the cell,
  so the GR-verbatim empty-taps default constructs-and-raises loudly with
  the compose `Upsampler(L) → FIR(taps, decimation=M)` workaround (which is
  phase-0 aligned — the raise message says so).
- **Harness gotcha:** GR's gcd(L,M)>1 user-taps info line goes to STDOUT and
  corrupts `run_gnuradio_ref`'s JSON channel —
  `gr.logging().set_default_level(gr.log_levels.off)` in the snippet.
- Tolerance: derived Q15 amplitude tolerance from `op_count=len(taps)`
  (max observed 4 LSB at N=5 vs tol 6); DUT additionally bit-exact vs
  `process_reference_q15` in every case. Saturation: RATE_1IN (feed-forward,
  no feedback corridor / reconvergent fan-in — INV-19/20 N/A). Orientation:
  shared gate + a full-burst 8-orientation check in the block's own suite.

---

## AGCCCBlock — complex AGC; feedback landings must be STATE registers; big rings fold ≤7 wide 2026-08-16

GR `analog.agc_cc` VERBATIM (rate/reference/gain/max_gain; semantics pinned
LIVE first: `out = in*gain` THEN `gain += rate*(ref − |out|)`, TRUE complex
magnitude, first sample scaled by the initial gain, `max_gain=0` = unclamped,
no lower clamp). 20 cells, ONE serpentine RING (7×5 perimeter = exactly 20):
`hold` (gain state + fed-back increment) → `tap` (2-rail MULQ, the block
OUTPUT, mid-block dual-FACE = the Costas `rotate` idiom) → the PROVEN
ComplexToMag CORDIC chain VERBATIM (pre1/pre2m/xy0..13/mag now shared
builders in `cordic_blocks.py`) on the EMITTED output words → `upd`
(rate·err with the RMS error-feedback accumulator + backward ginc WRITE +
WRITE.CFG lock-clear, @1 abutment — the QPSK-Costas pd_pi→phase shape, no
transit cell). INV-19 serialize-LOCK default ON. 1 attempt, 38-test suite +
saturation/orientation/legality/binding gates green. Derived settled-tail
tolerance 24 LSB (20 CORDIC-mag transfer + 2 gain dither + 1 MULQ trunc + 1
warm-up residual; measured peak 11); derived warm-up `ceil(10/(rate_eff·amp))`.
Durable lessons:

- **A FEEDBACK-LANDING register must be a pinned STATE var, NEVER an input
  `Port` (NEW — latent in every brokered-input block).** `resolved_io` counts
  every input-role register as a host-injected operand, and `broker_plan`'s
  port-complex expansion relays ONE delivery per such register. With the
  increment landing declared as an input Port (the Costas `dphase` pattern),
  any orientation whose input corridor gets BROKERED relayed a stale third
  word into the feedback register EVERY sample — the loop ran silently OPEN
  (output == frozen-gain trace; found at cw³ by comparing against a
  rate≈0 run). Declaring the landing a pinned StateVar (`ginc`@R2, the hole
  below the data words) keeps the operand group at exactly [xi, xq]; the
  backward WRITE resolves to the state register by name (state names match
  before input names — the qpd hazard, used deliberately). NOTE:
  ComplexCostasLoop's `dphase` input Port carries the same LATENT hazard —
  its 4×2 fold just never gets its input brokered in the gate orientations.
- **Fold a big ring ≤ 7 wide on the 10-wide chip — 8 wide passes every
  identity gate and dies at 180°.** An 8×4 ring leaves 1-cell channels;
  under cw² the router (whose INV-32 own-block-broker guard forbids the
  short path) wrapped BOTH corridors around the die and diverted the input
  THROUGH the x16_out port cell — the port-cell divert (entry stamped on
  x16_out) does NOT deliver: silent zero output, route reports ok (this
  silent-ship class is CLOSED 2026-08-16 — used-port transit is now a NAMED
  failure; the ≤7-wide fold rule stands for ROUTABILITY). The same
  20 cells as a 7×5 perimeter ring leave a 2-column channel and every
  orientation routes cleanly. Residual: at anchor (1,1) the mirror_v+cw²
  orientation still forces the wrap (input cell adjacent to the output cell
  against the contested row-0 corridor); anchor (1,2) is clean for all 8 —
  the orientation gate now supports a per-case anchor (5th case element),
  the ComplexToMag/Arg saturation-anchor precedent.
- **`{write:port}`-FIRST is mandatory when an input lands in R0 and the cell
  computes before forwarding** (INV-33 sharpened): hold's first draft did
  `clamps…; MOVE R0, R{in:xi}; write` — but `R{in:xi}` IS R0, long since
  clobbered; the block forwarded its own gain as the I rail (tap computed
  g·g). The Costas `phase` cell's `{write:fwd_input}` as the FIRST
  instruction (WRITE emits and preserves R0) is the correct form.
- **The RMS error-feedback accumulator is direction-asymmetric under bare
  MULQ** — floor truncation zeroes only POSITIVE sub-LSB increments (a
  negative err still steps −1), so the no-accumulator mutant stalls ONLY in
  the RISING-gain regime (start gain below ref/amp; stalls ~10923 LSB short
  at rate_q=3). A falling AGC self-repairs and would false-pass the
  mutation — pin the stall with gain=0.05, not gain=1.0.
- **A 20-cell chain needs ~4k sim events/sample saturated** — the pipelined
  harness default 100k-event cap reads as "livelock" at 50 samples; it
  completes (and is BIT-EXACT vs per-sample) at 200k. The 16-sample generic
  saturation gate fits the default cap. Unlocked (`pipeline_lock=False`)
  saturated drive diverges in ~90% of words — the INV-19 hazard is real and
  pinned in the suite.
- **Regime mirroring for the GR golden** (the agc_ff lesson, sharpened): run
  GR at the CHIP-QUANTIZED constants (rate_q/ref_q/gain_q/gmax_q as floats;
  `max_gain=0` → the Q15 ceiling 32767/32768) over the SAME quantized
  stimulus; the settled level is then rate-independent and the 137k-sample
  default-rate (3/32768) convergence is closed model-vs-GR with the chip
  linked by the bit-exact gate (a >argv-limit stimulus goes to the GR
  subprocess via a temp file, not argv).
## GolayDecoderBlock — SRAM-backed (24,12) syndrome decoder; the e_d-only LUT word and the harmless value-0 collision 2026-08-16

Extended Golay (24,12) hard-decision syndrome decoder (24:12 rate-compressing,
raw 0/1 words, tier 3, NO GR counterpart) — SRAM-backed (INV-31), built
entirely against the GolayEncoderBlock convention pin (`encode_word()` /
`_column_mask()`; B never re-derived). 45 tests, delay 0, tol 0, first
attempt green. Durable lessons:

- **STORAGE FORMAT (the design call, stated loudly): ONE panel word per
  populated syndrome — the 12-bit DATA-half error pattern e_d.** The
  manifest offered 2s/2s+1 double words or a packed descriptor for the
  24-bit pattern; neither is needed because the block emits ONLY the 12
  corrected data bits, so the parity-half pattern e_p is dead state.
  2026 words populated (2324 non-zero weight-≤3 patterns minus the 298
  parity-only ones) of the 4096-address space; single push-read per
  codeword, no double-read sequencing.
- **The stored-value-can-be-0 trap (CHAR_OFFSET lesson) can dissolve by
  DESIGN instead of by offset.** With e_d-only storage, a read of 0 is
  shared by s=0, parity-only correctable errors, and uncorrectable (≥4)
  syndromes — and all three need the SAME action (XOR nothing). The
  collision is semantically harmless, so no offset; gated explicitly
  (parity-only syndromes proven absent from the image AND exact through
  the chain). GENERAL RULE: before adding an offset, check whether every
  value-0 collision demands the same downstream action.
- **The s==0-skips-the-lookup idea was replaced by a UNIFORM single-path
  lookup:** address 0 is GUARANTEED unpopulated (syndrome 0 ↔ e=0, never
  stored), so a clean codeword's read returns 0 == no correction. One
  path = no clean/dirty fork in the correct cell (8 instructions), uniform
  timing, and no separate direct-kick corridor to the emit cell to verify.
  Consequence for SHARED panels: the LUT must own its full 4096-address
  region (a foreign word inside it would masquerade as an error pattern).
- **Known limit, proven not hand-waved:** weight-4 patterns can NEVER alias
  a correctable syndrome (XOR with a weight-≤3 pattern would be a weight-≤7
  codeword; d_min=8) — verified EXHAUSTIVELY over all C(24,4)=10626
  patterns — so exactly-4-error words always pass the received data half
  through (no miscorrection). Weight ≥5 CAN miscorrect (bounded-distance);
  demonstrated with 5 ones of a weight-8 codeword.
- **Budget arithmetic for a 3-forwarded-word LOAD-table pipeline:** the
  syndrome re-encode carries (D, P, partial Q) between cells, so the
  encoder's 7/5 mask split shrinks to 5/4/3 across THREE syn cells (syn2
  pays an entry MOVE for the partial-Q copy; syn3 trades a mask slot for
  the final `XOR q, p`). Keeping D and P in their (non-R0, explicitly
  placed) input registers UNTOUCHED through the loop — `AND R0, R{in:dw}`
  directly — saves a state register + entry MOVE per cell. The encoder's
  silent input-register/instruction collision is now an explicit gate
  (test_register_layout_no_silent_collision: inputs+state strictly between
  data top and 31−n_instr, every cell).
- **Full-chain panel harness for a MULTI-CELL driver chain (extends the
  Varicode two-chip pattern):** chip A = pack→syn1..3→correct with manual
  ResolvedTargets (@1 abutment, port-exit hop = W−x); the correct cell
  PARKS D in a panel SCRATCH register (R7 — with addr_regs=1 only R5 is
  address, so high regs are pure storage) via its real routed egress,
  time-ordered before the R5 address + R1 trigger; the intercepted
  push-read then carries dev.reg(7) to the emit chip alongside the real
  injected push. The parked-scratch word is how a panel round-trip can
  transport SIDE data without a second port.
- **The 24-bit group accumulator is one cell:** count down from 24, latch
  the data half at count==12 (`CMP count, twelve`), forward both at 0;
  stale bits climb above bit 11 of BOTH halves (D latched with garbage
  above 11, P's bits 12..15 = top of D) and are killed by ONE
  `AND 0xFFF` on the syndrome + the emit peel — the masked-read
  invariant covers a two-word stream, not just one.
- Per-sample panel contract (server forces per-sample for panel designs) →
  saturation NEEDS_BESPOKE with that reason; also removed a stale duplicate
  `VaricodeDecoderBlock` NEEDS_BESPOKE key (the second, stale QUARANTINE
  string was silently winning the dict literal).
- Metric: raw-word BIT-exact, delay 0, tol 0 (byte blocks are never Q15).

---

## MultiplyCCBlock — the 4-operand CO-RESIDENCY wall disproven: full complex product of two external streams 2026-08-16

GR `blocks.multiply_cc` (2 complex streams, elementwise): `yi=ai*bi−aq*bq`,
`yq=ai*bq+aq*bi`. Unlike add/sub_cc the math does NOT separate per rail —
each rail needs ALL FOUR operands — so this was the historically-predicted
hard case of the pair-blocks wave. Result: first-try bit-exact on chip,
race-free under saturated drive; 40 tests green (`test_multiply_cc.py`),
driven through `run_block_dut_complex2`/`_pipelined` UNCHANGED (the AddCC
driver reuse worked exactly as designed).

- **CO-RESIDENCY PLAN (the durable part): operands meet ONCE — in the
  landing cell's STATE — and only PRODUCTS travel.** `prods` (landing) runs
  the AddBlock counting-join tail verbatim, then snapshots `aq/bi/bq` to
  state with ONE read each (the ComplexMixer stale-latch trap: the next
  sample's packets can land in the input registers mid-compute; every
  operand here is consumed by TWO products, so unsaved double-reads would
  race under saturation). `ai` needs NO extra save — the join's own `jsav`
  R0-save IS the ai snapshot (on the fire path jsav always holds the fresh
  ai, whatever the packet order), a free register+instruction. Four MULQs
  from state → 4 WRITEs + 1 JUMP to `combine` (a 4-value one-burst forward
  is proven substrate practice — MMTimingRecovery forwards 5). 31/32 words.
- **HEADROOM: S=0 is the DERIVED optimum for signal×signal.** Both factors
  are Q15 signals ⇒ every `MULQ` product is ALREADY in `[-1,1)`; only the
  per-rail combine of two full-scale products can leave range, and a single
  16-bit ADD/SUB overflow is exactly recoverable from V + the saved
  MINUEND's sign (the AddCC restore; ADD overflow ⇒ equal signs ⇒ either
  operand's sign works). So NO prescale at all — the MultiplyConstComplex
  `/4`+`<<2` pattern the manifest suggested exists because CONSTANTS can
  exceed Q15 (|k|<2); signals cannot, and skipping it kills the 2^S error
  amplification AND the whole sat-shift cell. Rails saturate on overload
  exactly like a Q15 clip. `combine` = 2× (save-minuend / op / BR.NV +3 /
  3-instr sign restore), 25/32 words, 7 free for the INV-17 fan-out JUMP.
- **Derived tolerance 3 LSB, measured I=1 / Q=2.** Per rail two truncating
  MULQs (each err in [0,1) LSB toward −inf): I = p1−p2 ⇒ errors partially
  cancel, err ∈ (−1,1); Q = p3+p4 ⇒ they stack, err ∈ (−2,0] —
  `q15_quant_floor(op_count=2, S=0)=3` covers both + comparison
  quantization. KEY TRICK: SNAP the GR-equivalence stimulus to the Q15 grid
  (`round(x*32768)/32768`) so GR's float golden computes over EXACTLY the
  chip's words — input-quantization error (up to ~1.4 LSB extra at 0.7
  amplitude for a product) then cannot stack on the floor, keeping the
  derived tolerance tight instead of padded. Generalizes to any
  signal×signal block.
- **Wrap corner (the MultiplyBlock `(−1)·(−1)` class), pinned:** only
  `0x8000*0x8000` wraps MULQ. `a=b=−1−1j` makes all four products wrap;
  `yi = p1−p2 = 0` — the wraps CANCEL and I is still correct! — but
  `yq = p3+p4` pins to −full where GR gives +2 (would clip +full). Pinned
  bit-exact vs the block's own wrap-modelling reference; GR-equivalence
  stimulus bounded |a|,|b| ≤ 0.7 never reaches it.
- **Mutation teeth for a product:** swapped STREAMS is vacuous (commutative
  — asserted EQUAL, documented), so the teeth are: DROPPED-CROSS-TERM
  golden (yi=ai·bi, yq=aq·bq — a non-rotating separable fake) and
  SIGN-SWAPPED-CROSS-TERM golden (GR fed conj(b) — the correlator, not the
  product) — both FAIL the correct DUT; plus wrong-second-stream, swapped
  I/Q rails, inverted, +1 delay, empty. Same reasoning fixed the saturated
  non-vacuity probe: conjugate b (don't swap streams) to prove the queued
  drive is real. Three rotation gates (pure-j = analytic 90° swap, 180°,
  45°) prove the rotation live on chip.
## GolayEncoderBlock — extended Golay (24,12) systematic encoder, BIT-EXACT; the silent input-register/instruction collision 2026-08-16

Extended binary Golay (24,12) systematic hard-decision FEC encoder (12:24
rate-expanding, raw 0/1 words, tier 3, NO GR counterpart — golden = G = [I12|B],
B = the MacWilliams & Sloane 1977 Ch.2 §6 bordered reverse circulant, printed
verbatim in the class docstring). CONVENTION (pinned, shared verbatim with the
future GolayDecoderBlock): wire = `d11..d0 p11..p0` MSB-first, first arriving
bit = d11, p11..p0 = m·B with B column 0 → p11; executable pin
`GolayEncoderBlock.encode_word()`. B is SYMMETRIC (rows == columns) with
B·Bᵀ = I; the test certifies it by the EXHAUSTIVE weight distribution
1/759/2576/759/1 at weights 0/8/12/16/24 — the decisive fingerprint no wrong/
shifted/transposed B can fake. 19 tests, delay 0, tol 0. Durable lessons:

- **Budget the cells honestly BEFORE authoring (INV-7): the dispatch guessed
  ~3 cells, the real answer is 4.** One cell holding all 12 column-mask table
  words + the parity loop + I/O handoff needs ~35–40 of the 30 usable words;
  the 12-entry table MUST split across two cells. Final shape: pack (12-bit
  accumulate, 13 instr) → par1 (7 masks) → par2 (5 masks) → emit (two 12-bit
  bursts), a 2x2 serpentine (the RMSBlock fold), D+p forwarded as TWO words
  per hop (the RMS p/s two-word handoff, proven again here).
- **A non-R0 input register that collides with the instruction region is NOT
  rejected — it silently reads PROGRAM WORDS.** First build: par2 declared
  `pw` at register 12 while its 19 instructions resolved to 12..30. No
  resolver/build error; `{in:pw}` read instruction word 0x4009 (the cell's own
  first MOVE) as data, and BOTH upstream writes assembled to dest R0 (two
  identical `0x63c0` WRITE words) so par2's d got par1's p. Symptom: correct
  burst COUNT, garbage values with a CONSTANT bite (0x4009<<6 = 0x240) in the
  parity half. Forensics that cracked it in one pass: `read_cell_memory` dumps
  of all 4 cells after one group — the state registers named the wrong word
  instantly, no output-inference needed. Count non-R0 INPUT registers in the
  ≤30 budget, and keep them below `31 - n_instructions` AND outside the LOAD
  table range (the QAM16 aliasing trap has a new sibling).
- **The asymmetric 7/5 mask split is budget-forced:** par2 carries one extra
  entry MOVE (copy the incoming partial parity), so par1 holds 7 masks
  (9 data + 3 state + 18 instr = 30) and par2 holds 5 (7 data + 3 state +
  1 input + 19 instr = 30). Both cells resolve to exactly 30 words — legal.
- **The LOAD-table parity loop scales the P-flag idiom past the constant
  budget:** per bit `p = (p<<1) | parity(D & T[count])` with the down-counter
  AS the LOAD address (the HammingDecoder trick) — 12 masks cost 12 DATA words
  + ONE 10-instruction loop instead of 12 unrolled 4-instruction stanzas.
- **Deliberate no-reset with masked reads, again:** pack's D and both p
  registers are never cleared between groups; stale bits climb above bit 11
  and every read is masked (parity masks are 12-bit; the emit peel is
  `SHR #11` + `AND 1`). Covered by multi-group streams — and the stale-shift
  arithmetic ((p_prev<<6)|new matching the observed words EXACTLY) is what
  proved the mis-wiring hypothesis during debug.
- **Pick mutation stimulus that EXERCISES the mutated row:** the wrong-B-row
  gate (row 1 ← row 2) was initially green-on-mutant because all 4 stimulus
  words happened to have d10=0 (row 1 never used) — algebraically a no-op.
  The fixed set asserts the sensitivity precondition (`d10 ^ d9` set on some
  word) before trusting the gate. A mutation gate has its own INV-4 problem.
- Feed-forward 4-cell chain: saturation-safe with NO lock (RATE_1IN gate,
  saturated flat stream == per-sample), D4-invariant 8/8, placement-legal.
  Metric: raw-word BIT-exact, delay 0, tol 0 (byte blocks are never Q15).

---

## BlockInterleaverBlock — row-column matrix interleaver; the RUNTIME PATCH-SLOT computed store 2026-08-16

Bit-exact (metric=exact) rows×cols block interleaver/deinterleaver, 3 cells,
supported range `rows*cols ≤ MAX_DEPTH = 12` (RAISES beyond — the SRAM-panel
SCRATCH recipe is the documented, UNSHIPPED growth path). 60-test suite + all
gates green; 1 attempt. The durable lessons:

- **THE ISA HAS NO INDIRECT STORE — the RUNTIME PATCH-SLOT idiom supplies one
  (NEW, proven on simKYT).** `LOAD` is the only indirect op (read:
  `R0 = mem[mem[Rn]&0x1F]`); every WRITE/MOVE destination is an instruction
  field. A streaming interleaver must store each sample at a COMPUTED address,
  so: construct the instruction word at runtime — `0x63E0 | dest` is
  `WRITE @0` (HOP_CNT=31 = LOCAL store, dest field [4:0]) — and WRITE it into
  a known program slot of the cell that executes it; the slot (a HALT at
  build) then runs as the computed store. Cost: 1 ADD + the patch WRITE in the
  producer, 1 slot instruction in the executor. Verified end-to-end incl. the
  patch arriving as an EXTERNAL WRITE from the neighbouring cell, under
  saturated back-to-back drive, in all 8 D4 orientations (a memory ADDRESS
  does not rotate — no is_face words needed). Derive the base constant by
  assembling `WRITE @0, 0` (0x63E0), don't hand-encode.
- **Resolver fix that makes the idiom routable:** a dst input `Port` pinned
  INSIDE the instruction range (the patch slot) was reclassified "instruction"
  by `classify_addresses`' final sweep, so the router's `_resolve_named_input`
  couldn't see it and silently fell back to the cell's FIRST input register —
  patch delivered to R0, zero output, no error. `resolver.py` now lets an
  explicitly-declared input keep its role inside the instruction range
  (data/state keep instruction-wins). Regression:
  `test_block_interleaver.py::test_pinned_input_in_instruction_range_keeps_input_role`.
- **HARNESS: honor the build's `input_landings` (a brokered input corridor is
  LEGAL).** At one orientation (mirror_h+cw+cw) the router legitimately ended
  the port→block corridor at a BROKER cell abutting the block (turn program
  delivers into the landing); `run_block_dut`'s `len(route)`/manhattan hop
  consumed the burst AT the broker with the BLOCK's entry → zero output that
  looked exactly like an orientation failure (INV-23 failure-mode-4 class).
  Fix: `run_block_dut` now prefers `bres.chips[0].input_landings["in_blk"]`
  (the LIVE production contract: cell/entry/hop/data_addrs, resolving BOTH the
  ride-straight and brokered shapes) and falls back to the old derivation.
  When a rotated multi-cell block gives zero output, check whether the input
  net ends ON the landing cell or one short at a broker BEFORE suspecting the
  block.
- **One column walk IS the transpose permutation — and its wrap detects the
  block boundary for free.** Reading a row-major r×c buffer column-by-column
  is `addr += stride; if addr >= N: addr -= (N-1)` with stride = cols
  (interleave) or rows (deinterleave) — one machinery, both directions.
  BOUNDARY IDENTITY (saved a whole counter + 2 DataWords): a wrap lands
  exactly ON `stride` iff it wrapped from `N-1`, the last read of a block
  (wrap value `ra+stride-(N-1) == stride ⟺ ra == N-1`), so `CMP ra, stride;
  BR.NZ` after the wrap is the entire end-of-block detector. Verified
  exhaustively (pure-Python walk == sigma for every legal config) before
  authoring assembly.
- **Budget split that fits (3 cells, 1×3 column, I/O co-located):** `rgen`
  (read-address walk, 18 instr + 4 data + 2 state) → `wctl` (sequential 2N
  ring write pointer + patch construction, 13 instr) → `store` (2N-word
  ping-pong buffer @2..1+2N + a 4-instruction engine: patched slot, LOAD,
  write, jump; the slot IS the entry). Store capacity sets MAX_DEPTH:
  1+2N ≤ 25 → N ≤ 12. Tricks: accumulator delivery of the sample into store's
  R0 as wctl's LAST write (INV-33) freed one input register (N 11→12); ALL
  store consumption (slot, R0, LOAD ra) happens BEFORE the potentially
  backpressured `{write:out}`, so a stalled egress can't be overtaken by the
  next sample's deliveries — that ordering is the saturation-safety argument
  (proven: saturated == per-sample bit-exact incl. full-depth 12×1).
- **Golden discipline for a no-GR block:** cite the coding text (Sklar ch. 8;
  Lin & Costello) + state the write/read order LOUDLY + validate the
  reference IN-TEST against an independent numpy reshape/transpose
  formulation before holding the DUT to it. Burst-dispersion: the TRUE
  guarantee is "a burst of ≤ rows consecutive channel symbols corrupts AT
  MOST ONE symbol per row-codeword after deinterleave" (row ranges of a
  ≤rows-long read window are disjoint) — the manifest's older "≥ rows apart"
  phrasing is NOT a theorem (cross-column pairs can be closer); proven
  exhaustively + demonstrated on-chip with a corrupted channel stream.
- **The poc file (INV-25) was archaeology only:** its "V2" single-cell
  fill/drain design required externally-addressed writes (a harness hack, not
  a streaming block) and its 4-cell V1 was a non-functional sketch. Replaced
  wholesale; the catalog `_EXCLUDED_BLOCKS` entry (poc-era) lifted.
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

## GRC "Generate" overwrites same-named hand-written modules — INV-43  2026-08-26

GRC's **Generate** writes `<flowgraph id>.py` into the `.grc`'s own directory. If
a HAND-WRITTEN module already occupies that name, Generate destroys it silently,
and the loss reads as an ordinary modification in `git status` — so it lands in
whatever commit is open at the time.

This happened **twice**, found by two different routes:

* `examples/gru_classifier/gru_classifier.py` — a 534-line design module
  (topology, anchors, the on-chip runner) replaced by its 273-line generated
  flowgraph. Every symbol `build_kyt.py`, the demo and the gates import went with
  it; the suite went to 23 failed / 8 errors while the README advertised it green.
* `examples/fft128_2p2s/fft128_2p2s_demo.py` — a hand-written headless debugging
  vehicle (`--samples`, per-trigger yield, carrier-link traffic, word-for-word
  compare) replaced the same way. Found only because its README documented a
  `--samples` run that nothing in the tree could still perform.

**The rule:** a `.grc`'s flowgraph id must never equal the name of a hand-written
`.py` in the same directory. The repo convention is `<name>_demo` or
`<name>_grc`. When a `gen_grc.py` is the generator of record, change the id
THERE too — changing only the `.grc` reintroduces the collision on the next
regeneration.

**Gated by** `test_examples_grc_valid.py::test_grc_generate_target_is_not_a_hand_written_module`,
which checks all shipped `.grc`s: Generate may only target a file carrying GRC's
own generated-file banner. That banner is what Generate itself stamps, so the
test asks the only question that matters — would Generate overwrite something it
did not write?

**Corollary, and the deeper fix:** generated flowgraphs are no longer tracked at
all (see `.gitignore`). Tracking build output beside hand-written source is what
made both losses invisible. Nothing in the repo consumes the checked-in `.py` —
`grc_userpath_run.py` regenerates into a tempdir from the `.grc`.

## A display frame reader must drop each burst's ragged tail  2026-08-26

When `burst_len` is not an exact multiple of the frame stride, every burst ends
with a partial frame. A reader that keeps consuming across the boundary builds
its next "frame" from those leftover samples plus the head of the *next* burst's
zero-fill transient — an all-zero output — and with `server_repeat` looping the
batch it recurs forever in a regular good/good/blank cycle.

Measured on `fft128_2p2s`: `burst_len` 384 against `latency + 2*n_fft` = 383
leaves exactly one sample over, producing a blank plot on every third frame
across 4728 frames.

**No bit-exactness assertion can see this** — the DATA is correct; the fault is
in the framing of the display glue. It is caught only by tapping the display
block's own output (what the sink is actually painted with) rather than stopping
at the kyttar sink. Same lesson as the CSS transceiver's display gate.

## Splitting a cell buys a FACE, not just words — LZ4DecoderBlock  2026-08-29

`LZ4DecoderBlock` is `done`: the auto-placed, routed, built design decodes real
LZ4 blocks byte-exact on a real chip through a real `SramPanelDevice`, including
blocks produced by the **reference C compressor**. It had been stuck for three
passes behind a budget of exactly one word. Four things are worth keeping.

**1. The wall was a FACE COUNT, and the fix was an extra CELL.** A cell serves
ONE direction free; each extra one costs an in-program flip (2 instructions + 1
`is_face` DataWord). The emit cell had to serve three — the ring-forward to the
router, the panel, and the output corridor — i.e. 6 words against 5, exhaustive
over three placement windows. Moving ONLY the egress to an 8th cell dropped it to
TWO directions, and placing that cell BETWEEN the emit cell and the controller,
resting toward the controller, collapsed those two into ONE flip: the burst
reaches cell 7 at hop 1 and the controller at hop 2 through it. Thirteen of the
fold's fifteen internal edges then ride resting faces with no flip at all. That
is INV-46's "prefer more cells doing less" paying off in FACES rather than
instructions — the axis that was actually short.

**2. Three substrate facts, each measured, each the opposite of the cautious
guess** (`proto_*` harnesses, all on real simkyt):

* **An OCCUPIED cell is TRANSPARENT to a hop-counted word.** A cell with a real
  program forwards a transiting word on its own face without executing; only a
  word that LANDS (HOP_CNT == 31) runs the program. This is what makes the
  "put the egress cell in the middle" trick legal at all.
* **A cell may FLIP while words TRANSIT it.** Measured: 180 concurrent transits
  across a cell running a flip/write/restore burst, ZERO losses or misdeliveries.
  The race everyone assumes is there is not there, and assuming it is rules out
  the whole class of layouts that actually work.
* **A blank cell faced across the walk DOES deflect.** The egress cell is not
  transparent — that half of the folklore is true, and it is why the egress must
  belong to a cell whose walk can afford to end there.

**3. The end-to-end gate found THREE defects nothing else could.** Every per-cell
chip gate, the whole FSM model, the golden and the reference-C cross-check all
passed while the placed design was wrong in three separate ways:

* cell 4 kicked the emit cell's `emit_mat` (the panel RETURN door) instead of
  `fetch`, so every match began by re-emitting the previous literal;
* the OFFSET cell — which is the landing cell for the WHOLE match phase — dropped
  the match-length CONTINUATION byte once `nb` hit 0, so every match longer than
  18 bytes stalled (i.e. most real text);
* the egress WRITE was aimed at the first corridor cell (`@1`) instead of through
  the corridor, so the byte parked one cell out and the port stayed silent while
  the panel window filled perfectly.

Each is now a mutation gate that corrupts the REAL block and rebuilds on chip.
The lesson is the old one, sharper: a per-cell gate proves a cell, a model proves
an algorithm, and NEITHER proves a design. Only the placed, routed, built chip
does.

**4. Budget arithmetic in a test must be a POSITIVE assertion.** The suite used
to assert the emit cell had FEWER free words than a flip costs — a gate that
passes precisely while the block is broken and fails the day it is fixed. It is
now the reverse (every cell resolves; the flip is present and restored on both
bursts), which is strictly stronger and cannot rot into a pinned wall.

**Gated by** `verification/tests/test_lz4_decoder.py` (62 tests, no skips):
golden vs reference C both ways, the FSM model, per-cell chip gates, a real match
copy through a real panel, 8 end-to-end payload classes on the placed design,
reference-C blocks on the placed design, three whole-chip mutations, and the 8
D4 orientation cases.

## INV-50 is CLOSED by TWO passes at once, and the naive fix is wrong THREE ways  2026-08-29

**Merged with the `ChaCha20KeystreamBlock` pass, which closed the other half of
this independently and landed first.** The reconciled rule lives in INV-50; this
entry records the LZ4 half and what the merge taught. Read the two together —
neither half is sufficient, and the reason is structural:

> A walk is *"leave the source on face F, then turn at every occupied cell."*
> **ChaCha fixed F** (which face this PORT departs on — `emit_faces()`, declared
> as a neighbour CELL ID so it is orientation-correct by construction).
> **LZ4 fixed the turns** (which face each TRANSIT cell forwards on —
> `cell_faces`, from the model placement, likewise already rotated).
> A fix with only one half still returns wrong numbers, in different cases.

`router._get_routing_distance` fell back to Manhattan distance when its face-walk
missed, silently returning a hop that lands the word on the wrong cell. Closing
it needed three corrections, and the first two each looked right and broke a
shipped block:

* **Walk the block's AUTHORED faces, not the cell_map's.** At internal-resolution
  time the cell_map holds the router's positional guesses; the caller's authored
  faces are applied later. New `BlockDefinition.cell_faces`, filled from the model
  placement — which means they arrive already orientation-transformed, so the
  rotation INV-23 demands is correct BY CONSTRUCTION rather than by a second pass.
  (This is the same trick `emit_faces` uses from the other direction: never store
  a DIRECTION that a rotation can invalidate — store something the placer has
  already transformed, and derive the direction from it.)
* **Do NOT take the resting face just because it delivers.** A fold is usually a
  closed walk, so the resting face reaches an abutting neighbour the long way
  round — `LMSEqualizerBlock`'s `(2,1) -> (2,2)` is 1 hop SOUTH and 13 hops around
  the serpentine. Charging 13 sent the word past its target; LMS went from 14
  passed to 6 failed.
* **Do NOT take the shortest of all four faces either.** That hands a
  NON-flipping cell a hop it has no way to take — LZ4's `token -> matchlen` is
  1 hop SOUTH that the cell cannot use, against the 3 its resting walk costs.

The rule that survives all three: a DECLARED emit face (`emit_faces()`) wins
outright and nothing else is tried; otherwise the resting face plus **only the
faces the cell's own program DECLARES** via `is_face` DataWords (rotated by the
block's orientation), shortest wins — and a DIRECT ABUTMENT is always 1, because
a dual-face output cell's tap direction is not an `is_face` word at all (it is
filled in later from the drawn route, `build._apply_rotate_tap_face`), and
refusing that edge broke `CoherentRXBlock`.

**Why declaration beats inference, and why the merge kept both:** inference knows
which faces a cell HAS; only a declaration knows which face a given PORT uses. So
`emit_faces` is authoritative where present — a block that declares an edge and
gets it wrong should see the error, not have the router quietly find another face
that happens to work. Inference remains for the ~116 blocks that declare nothing,
and it is what closed ChaCha's own residual: taking the shortest over {resting} ∪
{declared flip faces} means a resting walk no longer wins merely by *arriving*,
which was the "spurious success" case that pass named as the worse half.

Six of LZ4's fifteen internal edges had a wrong hop before the fix; ChaCha
measured 6 of 22 flipped edges wrong across 233. The Manhattan fallback still
stands for block→block and block→port edges, where the cell_map genuinely holds
drawn-route faces; for a DECLARED internal edge it now raises a named
`RouterError` instead, while the undeclared positional default keeps the estimate
by choice.

**One code path was dropped in the merge, no behaviour:** `_get_routing_distance`
briefly carried two copies of the walk loop (one for the `start_face` case). It is
one walk with a different first step, so it is written once.

Verified after the merge: `placekyt/tests/` 1219 passed (baseline),
`test_orientation_invariance.py` 364 passed, `test_lz4_decoder.py` 63 +
`test_chacha20_fixed_tap_ring.py` / `test_chacha20_keystream_golden.py` 111
passed together, and 1040 passed across the panel/FFT/Costas/rendezvous families.
