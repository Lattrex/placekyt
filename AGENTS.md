<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# AGENTS.md — autonomous block-building guide

This is the front door for an **automated agent** (any harness — not tied to one
vendor) working in this repository. It is self-contained: read it top to bottom and
you have everything you need to build and verify a Kyttar DSP block on your own.

> **Your mission, by default:** take the next unbuilt block from the work-queue,
> author it, verify it is a drop-in equivalent of its GNU Radio counterpart, record
> what you learned, regenerate the status dashboard, and commit. Then repeat. The
> whole loop is defined in [§3 The block-building loop](#3-the-block-building-loop).

If a human gave you a *different* explicit task, do that instead — this default
mission is what you do when you were turned loose with no other instruction.

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

1. **`verification/KNOWLEDGE_BASE/invariants.md`** — INV-1…INV-7, the hard substrate
   rules (placement-dependent hop counts, params-dependent entry addresses, Q15
   saturation, the mandatory failing-mutation gate, the register budget, …). These
   are model-agnostic and apply across blocks. **Not reading these is the #1 cause of
   wasted time.**
2. **`verification/KNOWLEDGE_BASE/lessons_log.md`** — per-block lessons (newest
   first). If a similar block was done, its gotchas are here.
3. **`BLOCK_AUTHORING_GUIDE.md`** (+ `PROGRAMMING_GUIDE.md` for the cell model, ISA,
   Q15, and `@N` relative addressing) — how a block class is structured.

### Step 3 — Author the block

Blocks live in `runtime/python/gr_kyttar/placement/blocks/` (e.g. `gain.py`). Open a
shipped block of a similar shape and follow it. A block subclass provides:
`cell_count`, `interface` (entry address + I/O registers), `build_cell_programs()`
(the per-cell assembly), and `process_reference()` (a float reference used by the
test). Mirror the GRC block's **parameter names verbatim** and derive any
fixed-point/internal values from them — a user must never have to learn a
Kyttar-specific parameter.

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
- [ ] Manifest status is `"done"`; `gen_dashboard.py --check` exits 0.
- [ ] A `lessons_log.md` entry is appended.
- [ ] Any substrate limit hit is captured as an explicit guard test, not glossed over.

---

## 5. Where everything is (map)

| Path | What |
|------|------|
| `verification/manifest.json` | **The work-queue.** Block targets, GR counterparts, tiers, status. |
| `verification/KNOWLEDGE_BASE/invariants.md` | Substrate rules INV-1…N. **Read first.** |
| `verification/KNOWLEDGE_BASE/lessons_log.md` | Per-block lessons. Read relevant ones; append yours. |
| `verification/tests/test_gain.py` | The copy-me test template (DUT vs GR + mutations). |
| `verification/kyttar_verify/` | Harness internals: `dut_runner` (build+sim a block), `gnuradio_ref` (golden), `compare` (aligned, Q15-aware compare). |
| `verification/tools/gen_dashboard.py` | Regenerates STATUS.md from manifest + reports. |
| `verification/reports/<Block>.json` | Per-block measured metrics (generated by a passing test). |
| `STATUS.md` | **Generated** dashboard. Do NOT edit by hand. |
| `runtime/python/gr_kyttar/placement/blocks/` | Block source. One module per block. |
| `BLOCK_AUTHORING_GUIDE.md` / `PROGRAMMING_GUIDE.md` | How to write a block / the cell model + ISA. |
| `CONTRIBUTING.md` / `INSTALL.md` | Conventions / full install. |

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
