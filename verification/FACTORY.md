<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# The block factory — autonomous build orchestration + paper metrics

`AGENTS.md` defines how ONE agent builds ONE block. This defines how an **orchestrator**
runs that loop over MANY blocks, unattended, capturing the paper's workflow metrics
(tokens / turns / walltime / human interventions / prompts, per block).

The **product is the block library**; this factory grows it while measuring the cost of
growing it — the AI-workflow thesis of the paper.

## The pieces

| Tool | Role |
|------|------|
| `verification/manifest.json` | **The queue.** `planned` = ready (ascending tier); `in_progress` = claimed; `done` = shipped; `needs_human` = quarantined. |
| `verification/tools/factory_queue.py` | `ready` / `claim [--tier N]` / `set <B> <status>` / `status` — pick & mark blocks. `claim` prints the block's full entry (grc_block/tier/params) for the dispatch prompt. |
| `verification/tools/factory_dispatch.py` | **Prints the ready-to-paste builder prompt** for a block, filled from the manifest (`<block> \| --next \| --next --claim`). The single source of the methodology — hand its output to an AI agent. Anyone can run the factory on their own block with it. |
| `verification/tools/factory_metrics.py` | `record(FactoryRecord)` — writes `reports/factory/<Block>.factory.json` (the paper cost record). The ORCHESTRATOR writes this, not the builder. |
| `verification/tools/gen_paper_table.py` | Aggregates all `factory.json` into the paper table (per-block + per-wave + overall: tokens, walltime, autonomous-rate, interventions). |
| `verification/reports/<Block>.json` | The DSP-correctness record (dashboard source) — written by the block's own test, orthogonal to the cost record. |

## The orchestration loop (session-driven)

The orchestrator is the running agent session. One block at a time (or a small parallel
batch), it:

1. **Claim** the next ready block:
   `python verification/tools/factory_queue.py claim --tier 1`
   (flips it to `in_progress`, prints its manifest entry). Note `started_utc` and the
   token/turn baseline.
2. **Dispatch a builder** sub-agent (Agent tool; `isolation: "worktree"` so parallel
   builders don't collide on the tree; `run_in_background: true` for parallelism). The
   prompt = the DISPATCH TEMPLATE below, filled with the block name + grc_block + params.
3. **On the builder's return**, read its `<usage>` (subagent_tokens, tool_uses,
   duration_ms) — the exact, un-fudgeable cost. Determine outcome:
   - **Passed** (its test is green, mutation gate proven to fail, dashboard `--check`
     exits 0, block committed): `factory_queue.py set <Block> done` was done by the
     builder; record `verify_passed=True` + `commit_sha`.
   - **Quarantined** (hit a documented substrate wall — INV Q15/fold/RAM — or failed the
     mutation/equivalence gate after 2 attempts): `factory_queue.py set <Block>
     needs_human`; record `quarantined=True` + `quarantine_reason`. Do NOT keep grinding.
4. **Record** the FactoryRecord (`factory_metrics.record`) with tokens, turns
   (=tool_uses), walltime (=duration_ms/1000), attempts, human_interventions (count of
   nudges the orchestrator had to send), the prompts used, outcome, commit_sha.
5. **Refill**: go to 1 until the queue is empty or the token budget for the turn is
   spent. Batch-review `needs_human` blocks separately.

Resume with a `/loop` re-invocation: the manifest's `planned`/`in_progress` state IS the
resume point — a re-run picks up the next ready block.

### Parallelism

Dispatch N builders at once, each in its own git worktree (`isolation: "worktree"`),
each claiming a DIFFERENT block (claim before dispatch). The single orchestrator
serializes the manifest claims, so no two builders get the same block. Keep N modest
(the concurrency cap is min(16, cores-2)); Wave-1 blocks are cheap and safe to parallelize.

## Running the factory on YOUR OWN block (turnkey)

This is not only for the maintainer. To grow the library with a block *you* care about:

1. Add a `planned` entry to `verification/manifest.json` — `kyttar_block` (your block
   name), `grc_block` (the GNU Radio block it must match bit-for-bit), `tier`, `params`,
   and `poc: true` **only if** code for it already exists but was never verified (INV-25).
2. `python verification/tools/factory_dispatch.py <YourBlock>` — prints the exact builder
   prompt, filled in from the manifest. (`--next` picks the lowest-tier ready block;
   `--next --claim` also flips it to `in_progress`.)
3. Hand that prompt to an AI coding agent pointed at this repo. It follows `AGENTS.md`,
   authors + verifies the block against GNU Radio, and commits it — or quarantines with a
   named substrate wall.
4. When it returns, record the cost with `factory_metrics.record(...)` (tokens/turns/
   walltime from the agent's own accounting) and roll it into the paper table with
   `gen_paper_table.py`.

The prompt below is what `factory_dispatch.py` emits — kept here in prose as the human
reference. **If you change the methodology, change `factory_dispatch.py`'s `TEMPLATE`**
(it is what actually gets dispatched); keep this section in sync.

## Dispatch prompt template (per block)

> Follow `AGENTS.md` §3 to build and verify the Kyttar block **<KyttarBlock>** (GNU
> Radio counterpart **<grc_block>**, params **<params>**). Read the KB invariants FIRST
> (`verification/KNOWLEDGE_BASE/invariants.md`, `layout_rules.md`, `lessons_log.md`).
> Author the block, write its `verification/tests/test_<block>.py` from the `test_gain.py`
> template (edge + random ≥3 seeds + param-sweep + **mutation tests proven to FAIL**),
> add the complete GRC binding (INV-22), append a `lessons_log.md` entry, set the manifest
> status to `done`, regenerate + `--check` the dashboard, and commit to `main` (block
> source + test + report JSON + manifest + STATUS.md + KB entry, one commit; SPDX header
> on new files).
>
> **Definition of done** = every box in `AGENTS.md` §4 (green test, mutation gate FAILS,
> coverage, report JSON, verbatim GR param names, complete GRC binding, folded layout,
> orientation-invariant, legal footprint). Fix the BLOCK, never the gate; never loosen a
> tolerance to pass.
>
> **Stop + quarantine** (do NOT keep trying) if you hit a documented substrate wall — a
> Q15 dynamic-range limit that needs external RAM, a fold that can't stay ≤8-across, or a
> harness gap — OR the equivalence/mutation gate still fails after 2 real attempts. In
> that case: leave the manifest `in_progress`, write a `lessons_log.md` entry naming the
> exact wall, and END your run reporting "QUARANTINE: <reason>". Do not fake a pass.
>
> Return a one-line status: `DONE <Block> commit <sha>` or `QUARANTINE <Block>: <reason>`.

## Waves (build order)

- **Wave 1 — autonomous grind, clean metrics** (easy, single-cell, feed-forward): the
  tier-1 `planned` blocks + additions like AddConst, LFSR scrambler/descrambler, diff
  enc/dec, bit-pack/unpack, xor/and, CRC, threshold. No harness extension; racks up
  low-variance token/walltime numbers.
- **Wave 2 — verify the coherent-RX POCs** (the highest-value wave: these complete the coherent receiver chain):
  ComplexRRCMatchedFilter, ComplexCostasLoop, BPSKSlicer, MMTimingRecovery and
  GardnerTimingRecovery (both verified against `symbol_sync_cc`; Gardner took
  three attempts and two honest quarantines — see the lessons_log), then AGC
  variants, FLL, framing/access-code correlators.
- **Wave 3 — human-in-loop** (Q15-risky / needs RAM / >8 fold): a shared CORDIC
  (unblocks mag/arg/rms/atan/PLL at once), the LMS equalizer (NOT RLS), Viterbi ACS.
  These are dispatched supervised, not left fully autonomous.

## Guardrails (why a block quarantines instead of grinding forever)

The documented substrate walls (from the KB invariants + this session's analysis) that
force a stop rather than a human intervention loop:
- **Q15 16-bit accumulator** (no guard bits): deep accumulation / RLS / long correlation
  saturates → block-float or external RAM, else quarantine.
- **>~8-across fold** (INV-8/9): a multi-cell block wider than 8 silently fails to route.
- **Long-memory state** (interleaver depth, Viterbi survivor path, preamble reference):
  lives in external SRAM, not cell registers.
- **Amplification (gain>1)** needs integer-headroom staging, not plain Q15.

A builder that hits one of these quarantines with the exact reason — that reason is a
paper data point (which blocks the substrate can't yet host), not a failure to hide.
