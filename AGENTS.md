<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# AGENTS.md — autonomous block-building guide

This is the front door for an **automated agent** (any harness — not tied to one
vendor) working in this repository. It is self-contained: read it top to bottom and
you have everything you need to build and verify a Kyttar DSP block on your own.

> ## ⚠️ READ THIS FIRST — the working relationship
>
> **I am looking for a COLLABORATOR, not a cheerleader.** Honesty and integrity are the
> cornerstone values of this project. False promises are not appreciated and actively
> damage the work.
>
> **VERIFICATION IS KING. If it hasn't been verified, it isn't done.** This applies to
> blocks and — even more so — to examples and end-to-end chains. We must have the
> *correct* answer, not one that merely *looks or sounds* good and falls apart on first
> inspection.
>
> Non-negotiable rules:
> 1. **Do not claim success you have not demonstrated.** "It should work", "the build
>    passed so it's done", "each piece is verified so the whole is verified" — these are
>    NOT proof. Pre-claiming victory when it is demonstrably false is the single worst
>    thing you can do here. If you catch yourself about to write "proven"/"verified"/
>    "works", stop and ask: *did I actually run the whole thing and observe the correct
>    result end to end?* If not, say exactly what you did verify and what you did not.
> 2. **If you think you have the right answer, VERIFY IT.** Then verify it the way it
>    will actually be used — not a convenient proxy. A per-block test is not a
>    whole-chain test. A build-succeeds check is not a data-flows-through check. An
>    example is not done until it runs **end to end in the GUI / on the real placed +
>    routed chip** and produces the correct output (see §5b).
> 3. **Report failures plainly, with the evidence.** A precise "this does not work
>    because X, proven by Y" is far more valuable than a false "done". A quarantine or a
>    documented limitation with a reproduction is a real, respected result.
> 4. **When unsure, dig until you are sure, or say you are unsure.** Do not paper over a
>    gap with confident language. Confidence without verification is the failure mode
>    this project exists to eliminate.

> ## 🔒 HARD RULE — this is a PUBLIC repository. NOTHING private goes in it. EVER.
>
> Everything committed here is world-readable, forever. There is no "internal note",
> no "temporary comment", no "just this once". Before EVERY commit, check your diff
> against this list. Violating it is the one unrecoverable mistake — a leaked secret
> cannot be un-published.
>
> 1. **NEVER reference any Verilog — not file names, not module names, not content,
>    not directory layout.** Not in code, comments, docs, tests, commit messages, or
>    binaries. The hardware design does not exist as far as this repo is concerned.
>    Describe BEHAVIOR ("the hardware routing engine forwards on the cell's current
>    face"), never the source that implements it. This is absolute.
> 2. **Never name closed-source simulator internals** (source file names, module
>    paths, crate layout). The simulator ships as a binary; its insides are not a
>    citable reference. Rebuild the binary with path-prefix remapping + strip so it
>    carries no build-machine or source paths.
> 3. **Never cite private documents** — the architecture spec/notes by any name,
>    version, or section number; anything under `dev_docs/`; planning/roadmap/demo
>    material; agent-memory notes. If a fact matters publicly, state it in a public
>    doc (PROGRAMMING_GUIDE.md, doc/) and cite that.
> 4. **No business or strategy content**: conference/demo plans, unannounced targets,
>    hardware revisions, board/gateware internals, ROI/roadmap language, personal
>    initials or approver names.
> 5. **No absolute machine paths, ever** (`/home/<user>/...`). They leak the
>    environment AND break every other machine. Derive paths from
>    `Path(__file__).resolve().parents[N]`; write `<repo root>` in docs. Scratch/
>    debug scripts stay untracked (`.gitignore` covers `proto_*.py` and `dev_docs/`).
> 6. **Commit messages are public too.** Never mention private material, internal
>    incidents, or what was removed and why.
>
> If you are unsure whether something is private: it is. Leave it out and ask.

> **Your mission, by default:** take the next unbuilt block from the work-queue,
> author it, verify it is a drop-in equivalent of its GNU Radio counterpart, record
> what you learned, regenerate the status dashboard, and commit. Then repeat. The
> whole loop is defined in [§3 The block-building loop](#3-the-block-building-loop).

If a human gave you a *different* explicit task, do that instead — this default
mission is what you do when you were turned loose with no other instruction.

> **Building MANY blocks, or running this for someone else?** There is an
> orchestration + metrics layer on top of this loop — see
> **[`verification/FACTORY.md`](verification/FACTORY.md)**. It gives you a one-command
> queue (`factory_queue.py`), a copy-paste dispatch prompt anyone can hand to an agent,
> and per-block cost capture (tokens/turns/walltime/interventions) for measuring the
> workflow. **Read the whole of THIS file and the KB (§3 Step 2) before deciding what to
> build** — the manifest's `poc`/`planned`/`done`/`needs_human` states and the substrate
> invariants change what "build block X" actually means.

---

## 1. What this project is (one paragraph)

placeKYT places, routes, builds, and simulates DSP **blocks** onto an asynchronous
cell-array chip (simKYT is the bit-exact simulator). The **product is the block
library**: each Kyttar block is a 1:1 drop-in replacement for a GNU Radio Companion
(GRC) block — same name, same parameters — so a GRC design ports to the chip with
zero friction. Your job is to grow that library, one verified block at a time. The
chip is the vessel; the blocks are the value.

---

## 2. One-time setup (clean VM)

From the repo root, in a fresh checkout:

```bash
# 1. A Python venv for placeKYT + the verification harness.
python3 -m venv .venv
.venv/bin/pip install -r placekyt/requirements-dev.txt
.venv/bin/pip install -e runtime/python      # provides gr_kyttar + the simkyt simulator

# 2. GNU Radio is the GOLDEN REFERENCE. The harness shells out to a Python that has
#    GNU Radio installed (kept in a separate process so its NumPy never clashes with
#    the venv's). On a standard GNU Radio install that interpreter is /usr/bin/python3.
#    Confirm it imports:
/usr/bin/python3 -c "from gnuradio import gr, blocks, filter, analog, digital; print('GNU Radio OK')"
```

Point the harness at that GNU-Radio Python with `KYTTAR_GR_PYTHON` (defaults to
`/usr/bin/python3`). Everything below assumes these two are in place.

**Smoke-test the whole toolchain before doing anything else** — if this isn't green,
fix the setup, do NOT start authoring:

```bash
KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest verification/tests/ -q
```

Expected: all tests pass (the shipped `GainBlock` + `FIRFilterBlock` suites). This
proves your build path, the simulator, and the GNU Radio reference all work end to end.

---

## 3. The block-building loop

This is the loop. Run it once per block, then repeat.

### Step 1 — Pick the next block

The work-queue is **`verification/manifest.json`** (machine-readable; it is also the
source for the status dashboard). Each entry names a Kyttar block, its **exact** GNU
Radio counterpart (`grc_block`), a tier, and a status.

Pick the **first `"status": "planned"` block in ascending tier order** (tier 1 =
feed-forward/memoryless — start here; tier 2 = stateful/loop; tier 3 = a new block
with no exact GR counterpart). Lower tier = simpler = do it first. Skip `in_progress`
unless you are explicitly resuming it, and skip `done` / `wont_map`.

Set its status to `"in_progress"` in the manifest before you start.

### Step 2 — Read before you build

Read these IN ORDER. They will save you the exact mistakes that have already been
made and solved:

1. **`verification/KNOWLEDGE_BASE/invariants.md`** — INV-1…INV-10, the hard substrate
   rules (placement-dependent hop counts, params-dependent entry addresses, Q15
   saturation, the mandatory failing-mutation gate, the register budget, and the
   layout rules INV-8/9/10). These are model-agnostic and apply across blocks.
   **Not reading these is the #1 cause of wasted time.**
2. **`verification/KNOWLEDGE_BASE/layout_rules.md`** — REQUIRED if your block is more
   than one cell. How a block FOLDS on the array: input/output on the same edge so a
   bus can tap both, output egressing the *last* cell of a wavefront, and the ≤8-cells-
   across convention on this 10×12 chip. None of it is enforced by a DRC — a block
   that ignores it builds fine and then **silently fails to route** (no output, no
   error). This is exactly what trips up multi-cell blocks; do not skip it.
3. **`verification/KNOWLEDGE_BASE/lessons_log.md`** — per-block lessons (newest
   first). If a similar block was done, its gotchas are here.
4. **`BLOCK_AUTHORING_GUIDE.md`** (+ `PROGRAMMING_GUIDE.md` for the cell model, ISA,
   Q15, and `@N` relative addressing) — how a block class is structured.

### Step 3 — Author the block

Blocks live in `runtime/python/gr_kyttar/placement/blocks/` (e.g. `gain.py`). Open a
shipped block of a similar shape and follow it. A block subclass provides:
`cell_count`, `interface` (entry address + I/O registers), `build_cell_programs()`
(the per-cell assembly), and `process_reference()` (a float reference used by the
test). Mirror the GRC block's **parameter names verbatim** and derive any
fixed-point/internal values from them — a user must never have to learn a
Kyttar-specific parameter.

**If you are ADDING A PARAM to an existing block, re-check the fold.** A new param
(e.g. `order`, `complex`) that grows the cell count can turn a folded block into a
longitudinal strip — the exact shape that silently fails to route (INV-8). Layout is
resolved WITH the params (INV-6/11), so the *widened* variant must fold on its own:
I/O co-located on one bus-facing edge, even column count (INV-14), ≤8 across (INV-9).
Do not assume the base block's fold carries over.

### Step 4 — Verify it (the gate that defines "done")

Copy **`verification/tests/test_gain.py`** — it is the gold-standard template. Write
`verification/tests/test_<block>.py` that runs your block (built + simulated on simKYT
= the DUT) against its GNU Radio block (the golden reference) over **edge + random +
parameter-sweep** stimulus, comparing within the derived Q15 tolerance.

The acceptance bar (`verification/README.md` + INV-4): the suite is green **AND**
includes **mutation tests** that corrupt the DUT (invert output, wrong parameter, +1
sample-delay, empty output) and assert the gate **FAILS**. A gate never shown to fail
certifies nothing. Do not tune tolerances to pass — they are derived/locked.

Run it:

```bash
KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen \
  .venv/bin/python -m pytest verification/tests/test_<block>.py -q
```

A passing run should emit `verification/reports/<KyttarBlock>.json` (pass + metrics),
the way `test_gain.py` does — the dashboard reads it.

If it does not match: **fix the block, never the gate.** Find the root cause (the
invariants cover the usual ones). If you hit a genuine substrate limitation, record it
as an explicit known-limit guard test (see FIRFilterBlock's tap ceiling) rather than
claiming done — and report it. Never hide a problem behind a loosened tolerance.

### Step 4b — Bind it into GRC (a block is unusable without this)

A verified block that has no GRC binding shows up as a red **"Missing Block"** in GNU
Radio Companion and cannot be placed in a flowgraph — it is NOT done (INV-22). Add
both halves of the binding and make sure `install.sh` ships them:

1. **`gr-kyttar/grc/<id>.block.yml`** — copy an existing one of the same shape (e.g.
   `kyttar_costas_loop.block.yml`). It MUST expose **every** parameter under
   `parameters:` (same names/defaults/units as GR — INV-0) and list **every** input
   and output the block actually has for those params, including param-dependent ports
   (e.g. the yi/yq complex pair when `order=4`, a second rail, decimated output). A
   param that exists on the block but not in the YAML is a hidden param — the user
   can't set it.
2. **`gr-kyttar/python/kyttar/<name>.py`** — the shim the `make:` template calls (only
   needed if the block has one; follow `costas_loop.py`).
3. If the block needs a `_TYPE_OVERRIDES` / `_INSTANCE_PARAMS` entry in
   `placekyt/engine/grc_import.py` so the importer resolves its ports **with** its
   params (INV-6/11), add it.
4. Run `gr-kyttar/install.sh`, open the block in GRC, confirm: no "Missing Block",
   every param visible, ports match. A stale install shadows repo edits — re-run it.

### Step 5 — Record what you learned

Append a dated entry to **`verification/KNOWLEDGE_BASE/lessons_log.md`** (newest
first): what you tried, the derived tolerance, and any block-specific gotcha. If a
lesson generalizes across block classes, promote it to a new `INV-N` in
`invariants.md`.

### Step 6 — Update the queue + dashboard

Set the block's manifest status to `"done"`. Then regenerate the dashboard (it is
GENERATED — never hand-edit `STATUS.md`):

```bash
.venv/bin/python verification/tools/gen_dashboard.py            # rewrites STATUS.md
.venv/bin/python verification/tools/gen_dashboard.py --check    # must exit 0
```

### Step 7 — Commit

Commit directly to `main` (this repo's convention — no feature branches). Include the
block source, its test, its report JSON, the manifest change, the regenerated
STATUS.md, and the KB entry as one coherent commit. Keep the SPDX header
(`GPL-3.0-or-later`) on every new file.

Then go back to Step 1.

---

## 4. Definition of done (per block)

A block is DONE only when ALL of these hold — this is the bar an autonomous run must
not lower:

- [ ] `verification/tests/test_<block>.py` is **green**.
- [ ] It includes mutation tests proven to **FAIL** on a corrupted DUT (INV-4).
- [ ] Coverage = edge + random (≥3 seeds) + parameter sweep.
- [ ] `verification/reports/<KyttarBlock>.json` exists with measured metrics.
- [ ] The block's GRC parameter names match GNU Radio verbatim.
- [ ] **GRC binding exists and is complete (INV-22) — ENFORCED by
      `verification/tests/test_grc_binding_complete.py`:** the block has a
      `gr-kyttar/grc/<id>.block.yml` **and** its Python shim, the YAML exposes
      **every** parameter (matching GR names/defaults) AND every param-dependent
      port, `install.sh` copies both, and the block resolves in GRC with **no
      "Missing Block"** and no hidden params. A block with a passing verification
      test but no GRC binding is **NOT done** — it cannot be used in a flowgraph.
      This is a HARD GATE: `test_grc_binding_complete.py` fails for any done block
      without a resolvable, param-complete binding. Run it before marking a block
      done. A param the block INTENTIONALLY does not expose (a documented
      HW-deviation that raises) must be listed in the class's `GRC_UNSUPPORTED_PARAMS`
      tuple — that is the ONLY way to legitimately omit a param from the binding.
- [ ] **Layout is folded (INV-8/9/14):** if the block is >1 cell (or a new param
      *made* it >1 cell), its I/O co-locate on one bus-facing edge — it is NOT a
      longitudinal strip. Adding a param that grows the cell count means RE-folding.
- [ ] **Orientation-invariant (INV-23):** the block computes IDENTICALLY in all 8 D4
      orientations — `verification/tests/test_orientation_invariance.py` is green for
      it (drive it via the `orient=` param on the DUT runner in every orientation and
      assert the output equals the identity build). A block that breaks when rotated is
      broken. Its internal (`transit_*`) cells are FIRST-CLASS block cells (block color,
      footprint, same rules), never light-blue routing cells.
- [ ] **Footprint legal under orientation AND movement (INV-25):** the block's cells stay
      pairwise-distinct (no self-overlap) after every D4 orientation AND after user
      movement — a whole-block drag OR an Alt-drag single-cell breakout. This is DISTINCT
      from orientation-INVARIANCE (that's compute; this is geometry). A multi-cell block
      with an internal transit/relay cell can fold that cell onto a datapath cell; that
      self-overlap used to pass placement (the legality gate only compared DIFFERENT
      blocks) and fail only at DRC with a broken build + un-routable net.
      `verification/tests/test_placement_legality.py` must be green — add the block (with
      its footprint-growing params, e.g. `pipeline_lock=True`) to its `BLOCKS` list. Both
      the placer's `_placement_legality` and the single-cell `move_cell` reject overlaps
      (self or cross-block); if a new block introduces an internal cell, verify it can't
      be orphaned.
- [ ] **Saturation-safe (INV-19/INV-20):** the block produces the CORRECT output COUNT
      (its N:M rate, no dropped/duplicated samples) AND the correct VALUES when driven
      SATURATED — the whole burst enqueued back-to-back with NO inter-sample quiescence
      (`queue_words_physical`), the real GNU-Radio / hardware streaming condition. This is
      a REQUIRED gate, exactly like orientation: the per-sample harness (`run_block_dut`,
      inject-and-flush) HIDES feedback/handshake/fan-in hazards, so a block can pass every
      per-sample test and still collapse under load. `verification/tests/test_pipeline_saturation.py`
      must be green for it — the block is in one of its coverage sets (REAL_1IN / REAL_2IN /
      RATE_1IN / COMPLEX_2IN2OUT) OR in `NEEDS_BESPOKE` with a reason + its own saturated gate
      (no silent omission; a coverage test enforces this). Two known hazard classes: a
      data-only FEEDBACK loop that assumes inter-sample settle (INV-19: Costas/Gardner —
      fix = serialize-LOCK), and a feed-forward RECONVERGENT FAN-IN where arms of unequal
      length reconverge on one cell (INV-20: ComplexMixer/NCO/FrequencyModulator — fix =
      the SAME serialize-LOCK: landing cell LOCKs its arbiter, exit cell clears it via a
      backward `WRITE.CFG`; opt-in `pipeline_lock=True`). If your block has feedback or a
      reconvergent fan-in, it needs the lock — do NOT invent a new mechanism, port the
      ComplexMixer one.
- [ ] Manifest status is `"done"`; `gen_dashboard.py --check` exits 0.
- [ ] A `lessons_log.md` entry is appended.
- [ ] Any substrate limit hit is captured as an explicit guard test, not glossed over.

---

## 5. Where everything is (map)

| Path | What |
|------|------|
| `verification/manifest.json` | **The work-queue.** Block targets, GR counterparts, tiers, status. See the status note below. |
| `verification/KNOWLEDGE_BASE/invariants.md` | Substrate rules INV-1…N. **Read first.** |
| `verification/KNOWLEDGE_BASE/layout_rules.md` | How a multi-cell block FOLDS on the array (same-edge I/O, last-cell egress, ≤8-across). **Read before any 2+ cell block.** |
| `verification/KNOWLEDGE_BASE/lessons_log.md` | Per-block lessons. Read relevant ones; append yours. |
| `verification/tests/test_gain.py` | The copy-me test template (DUT vs GR + mutations). |
| `verification/kyttar_verify/` | Harness internals: `dut_runner` (build+sim a block), `gnuradio_ref` (golden), `compare` (aligned, Q15-aware compare). |
| `verification/tools/gen_dashboard.py` | Regenerates STATUS.md from manifest + reports. |
| `verification/reports/<Block>.json` | Per-block measured metrics (generated by a passing test). |
| **`verification/FACTORY.md`** | **Run the loop over MANY blocks (unattended) + the paper metrics.** Read this if you're building more than one block, or running the factory for someone else. |
| `verification/tools/factory_queue.py` | The queue over the manifest: `ready` / `claim` / `set` / `status`. |
| `verification/tools/factory_dispatch.py` | Prints the ready-to-paste builder prompt for a block (`<block>` / `--next` / `--next --claim`) — the single source of the build methodology. |
| `verification/tools/factory_metrics.py` · `gen_paper_table.py` | Per-build cost record (tokens/turns/walltime/interventions) + the paper table. |
| `STATUS.md` | **Generated** dashboard. Do NOT edit by hand. |

> **Manifest status — read this before picking a block.** `done` = verified BER-0 vs
> GNU Radio (trustworthy). `planned` = **on the queue** — and note: a `planned` entry
> may carry **`poc: true`**, meaning **code for it already exists but was NEVER verified
> against GNU Radio**. A PoC is NOT trustworthy: it may have a latent bug that only
> hides because the one place it's used happens to stay in range (this is exactly what
> happened to `ComplexGainBlock` — it wrapped instead of saturating on overload, caught
> only when it went through the gate). So "building" a `poc` block usually means
> *finalize + VERIFY the existing code*, not write it from scratch — and expect to find
> and fix real bugs. `needs_human` = quarantined (hit a substrate wall; a human must look).
| `runtime/python/gr_kyttar/placement/blocks/` | Block source. One module per block. |
| `BLOCK_AUTHORING_GUIDE.md` / `PROGRAMMING_GUIDE.md` | How to write a block / the cell model + ISA. |
| `CONTRIBUTING.md` / `INSTALL.md` | Conventions / full install. |

---

## 5b. Building EXAMPLES (not just blocks) — the end-to-end bar

An **example** (a modem, a transmitter, a demo under `examples/`) is a whole flowgraph
placed and routed on a chip, not a single block. It has a HIGHER verification bar than a
block, because "each block is verified" does NOT imply "the assembled chain works" — the
placement, the routing, the port hand-offs, the panel wiring, and the actual data flow
are all new surface area that only the whole chain exercises.

**An example is NOT done until it has been run END TO END, on the real placed + routed
chip, and observed to produce the correct output.** Specifically:

- [ ] **The `.kyt` genuinely builds AND routes as ONE chip** — every net routed, no gaps,
      no islands. `auto_pnr(...).ok` AND `build().ok` both true, and you have LOOKED at
      the result (cell adjacency / the drawn routes) to confirm the chain is actually
      connected, not visually disconnected fragments.
- [ ] **Data actually FLOWS through the placed topology.** Drive the real input, run the
      real simulator on THAT built bitstream, capture the real output, and assert it
      matches a golden. **Running each block separately and composing the results in
      Python is NOT a whole-chain proof** — it never exercises the placement/routing/
      hand-offs, which is exactly where examples break. If your "proof" would still pass
      with a broken `.kyt`, it is not proving the example.
- [ ] **The path the USER will actually use works.** If the example is meant to be opened
      in the GUI or imported from a `.grc`, then GRC import → auto-place → auto-route →
      run must succeed and produce the right output. Verify THAT path, in the GUI, end to
      end — not a headless proxy that skips the parts that fail. If you cannot run the GUI
      yourself, say so explicitly and mark the example UNVERIFIED-in-GUI; do not claim it
      works.
- [ ] **SRAM-panel-backed chains** (Varicode, CW keyer, any INV-31 block) must have the
      panel wired and routed in the `.kyt` (mirror `placekyt/engine/sram_demo.py` +
      `placekyt/tests/data/demo/sram_panel_demo.kyt`, which are FULLY hand-routed) and
      must flow data through the panel round-trip on the built chip. Note: GRC auto-route
      of SRAM panels is a known gap — check its status before claiming import works.
- [ ] **The `.grc` OPENS AND BUILDS under the real GRC compiler** —
      `verification/tests/test_examples_grc_instantiate.py` must pass for it. This
      GRC-generates the flowgraph against the REPO ymls and INSTANTIATES the generated
      top block with the REPO markers: it catches schema-invalid ymls (a missing
      `file_format` silently becomes "Missing Block"), dropped connections, invalid
      params, and marker-level ITEMSIZE mismatches the ymls can misrepresent (a yml may
      claim any dtype — the marker's `io_signature` is the runtime truth; keep them in
      agreement, and remember GRC in the GUI reads the INSTALLED ymls until
      `install.sh` re-syncs them). The static yml lint
      (`test_examples_grc_valid.py`) must also pass.
- [ ] **A demo flowgraph ships REAL stimuli.** A placeholder RX vector
      (`[0.0]*64`) passes every headless gate while showing the user NOTHING — the
      shipped `.grc`'s own embedded stimulus must exercise the chain, and for the
      transceivers `test_examples_grc_userpath.py` runs the SHIPPED `.grc` (GRC-
      generated, real GR interpreter) against the SHIPPED `.kyt` hosted on the GUI's
      default server port and asserts the decoded output.
- [ ] **The demo's scopes must actually DRAW the verified output.** Three GR display
      rules blanked whole windows while the data verifiably arrived: (1) a QT
      `time_sink` draws NOTHING until a FULL `size` buffer arrives, AND the GR
      scheduler STRANDS the tail of a finite stream (measured: a 200-sample burst
      delivers only 192 to the scope, an 8-char decode delivers NOTHING — even
      after WORK_DONE) — so a scope sized ≥ its burst NEVER paints. Either size the
      scope ≤ burst−16, or loop the display sink (`server_repeat=True` re-emits the
      genuine one-batch result — the shipped fix for the char scopes; assert
      repetition integrity in the gate). (2) `kyttar.sink` emits q15/32768 FLOATS —
      put a ×32768 rescale in front of any byte/ASCII-value scope. (3) An
      un-plotted time sink still renders a default-axis empty frame (1024/srate) —
      a "blank window with a plausible axis" means STARVED, and the axis arithmetic
      tells you the size the scope is actually running with. A headless gate cannot
      see a blank window; the pixel-probe (render the qwidget offscreen +
      `nitems_read`) is how to verify a scope truly draws.
- [ ] **No corridor routes THROUGH a DSP block's cells**
      (`test_kyt_route_transits.py`). Taps and mid-corridor deliveries use standard
      build BROKERS (plain routing cells; corridor words transit them at HOP<31); the
      ONLY cell class two corridors may share is a CrossoverBlock at a genuine
      crossing (one fwd_face per cell — a crossing is what crossovers are for).

If any box is not checked, the example is **not done** — say exactly which parts you
verified and which you did not. A "proven sample-exact" claim backed only by per-block
composition, with a `.kyt` that is actually disconnected, is precisely the false-victory
failure this project forbids (see the top-of-file mandate).

---

## 6. Hard rules (do not violate)

- **Never make a gate pass without understanding why.** A test that mismatches means
  the block (usually) has a bug — fix the block, find the root cause. Modifying a test
  or loosening a tolerance to go green is the single worst thing you can do here; it
  hides bugs. (INV-4.)
- **Tolerances are derived, never tuned to pass.** The harness computes a Q15
  quantization-aware tolerance; if a correct block exceeds it, the bug is real.
- **Report blockers immediately; never hide them.** If you hit a substrate limitation
  you cannot solve, record it as an explicit known-limit guard test and surface it —
  do not ship a quietly-simplified block that doesn't actually work.
- **Mirror GNU Radio exactly.** Parameter names and semantics match the GR block; any
  fixed-point internals are derived, never exposed.
- **Commit to `main`; SPDX header on every new file.**
- **Scope your searches to this repository.** Never search filesystem roots
  (`find /`, `find /home`, or any top-level scan) — it can hang for many minutes.

---

## 7. If you're a Claude Code agent

This file is the source of truth; `CLAUDE.md` just points here. Follow the loop in §3.
