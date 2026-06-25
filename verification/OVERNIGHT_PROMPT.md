# Overnight tier-1 block-build prompt (hardened v2)

Copy the block below verbatim to the clean-context VM agent.

---

```
/loop Build the remaining tier-1 GRC-parity DSP blocks autonomously, one per iteration, until the manifest queue has no "planned" tier-1 blocks left. Then STOP the loop for review. Work ONLY in /home/system/placekyt (the only repo on this machine). Use /home/system/placekyt/.venv. GNU Radio is at /usr/bin/python3 (export KYTTAR_GR_PYTHON=/usr/bin/python3). Commit directly to main, PLAIN messages, NO AI-attribution suffix (no "Co-Authored-By", no "Generated with", no emoji).

## The goal
Every Kyttar block must TRULY MATCH its GNU Radio counterpart: same name a user expects, same parameters a GRC user sets (frequency in Hz, cutoff in Hz, gain — NOT internal fixed-point words; derive those inside the block), same output within a quantization-aware tolerance. GNU Radio is the golden reference; the Kyttar block built on simKYT is the DUT. NEVER fake a match, NEVER weaken a derived tolerance to force green.

## The work queue (single source of truth)
verification/manifest.json. Work the tier-1 entries with status "planned" in listed order, ONE per loop iteration. When none remain, STOP. (If you think the manifest is stale, re-read it from disk — do not assume.)

## Per-block procedure (do ALL of it, then commit, then CONTINUE)
1. READ FIRST every iteration: verification/KNOWLEDGE_BASE/invariants.md and lessons_log.md. They encode hard-won substrate rules and per-block gotchas — they will save you hours and stop you from re-deriving (or wrongly declaring impossible) things already solved.
2. Study the GR block (the grc_block factory in the manifest) in /usr/bin/python3. Match its PARAMETERS exactly, in the user's units. Derive any Q15/freq_word/half-coeff internals inside the block.
3. Implement/fix the block in runtime/python/gr_kyttar/placement/blocks/. REUSE existing datapaths where the GR block is a specialization (a decimator IS an FIR + emit-every-M; a low/high/band-pass IS an FIR whose taps come from gnuradio.filter.firdes; a complex mixer IS multiply_cc fed by a signal source).
4. Write a verification test in verification/tests/ modeled on test_gain.py / test_fir_filter.py / test_iir_biquad.py: GR golden vs simKYT DUT via compare_against_grc, derived Q15 tolerance, edge + random + parameter-sweep stimulus, AND mandatory mutation/negative tests (corrupt the DUT — invert, wrong param, +1 delay — and assert the gate FAILS; per INV-4 a gate that can't fail certifies nothing).
   - For COMPLEX (I/Q) or LLR/soft blocks (NCO/sig_source_c, ComplexMixer/multiply_cc, SoftDemod, ComplexRRC): the harness now supports them — use run_block_dut_complex / run_gnuradio_ref_complex and the complex/LLR compare path (see verification/tests/test_complex_harness.py for the working pattern). You do NOT need to build the complex harness; it exists.
5. Run: cd /home/system/placekyt && KYTTAR_GR_PYTHON=/usr/bin/python3 QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest verification/tests/<your_test>.py -q  AND the full verification suite AND the placeKYT suite (cd placekyt && QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m pytest tests/ -q) — all must stay green.
6. Add/confirm the GRC palette mapping (.block.yml) with the LABEL matching GNU Radio's verbatim ("Low Pass Filter", "Signal Source", etc.).
7. Update verification/manifest.json: status -> "done", fill metric/params/notes (notes are the lasting record). Append a lessons_log.md entry. Promote anything general to invariants.md.
8. Regenerate the dashboard if verification/tools/gen_dashboard.py exists.
9. Commit this block ALONE (block + test + manifest + KB + GRC yml), plain message, then CONTINUE to the next planned block.

## Decisions already locked (follow exactly)
A. NCO/Mixer -> GRC-native params. NCOBlock mirrors analog.sig_source_c (sample_rate, frequency Hz, waveform, amplitude; freq_word = round(frequency/sample_rate*65536) internally; label "Signal Source"). ComplexMixer matches multiply_cc semantics. When you change a signature, grep and FIX every caller (demos, .grc, tests, generators) and prove the full suite stays green.
B. BUILD the four firdes filters (LowPassFilter, HighPassFilter, BandPassFilter, BandRejectFilter): pure-Python wrappers calling gnuradio.filter.firdes.{low_pass,high_pass,band_pass,band_reject}(...) for the taps, reusing the FIRFilterBlock datapath. Verify the taps are bit-identical to firdes AND the output matches fir_filter_fff fed those taps. (firdes is importable at /usr/bin/python3.)

## DO NOT GIVE UP — the continuation rule (this is what failed last time)
- This is a LOOP. A single hard block must NEVER stop the run. If a block resists, you SKIP it (revert its uncommitted changes with git checkout, set its manifest status to "blocked" with a notes= root-cause reason, append a lessons_log.md entry, commit ONLY that manifest/KB note) and CONTINUE to the next block. Independent blocks (the firdes filters, AddConst, MultiplyConstComplex) are easy — never let a hard block (a loop/decision block) prevent you from doing them.
- BEFORE you mark anything "blocked" or "impossible", you MUST: (a) re-read invariants.md for an existing technique, (b) try the obvious fixed-point-DSP creativity, and (c) state in the commit the EXACT mechanism that blocks it and what would unblock it. "It overflows" or "it's recursive" is NOT a root cause — show the number and the structural reason.
  - Worked example you can reuse: a coefficient with |c|>1 is unrepresentable in Q15. That is NOT a wall — store c/2 and apply its MAC/MSU op TWICE (INV-15). The IIR biquad "impossible" claim was wrong for exactly this reason; the real bug there was a silent clamp. Reach for that kind of move before quitting.
  - A genuine wall (mark blocked, with the number): something that truly needs a simKYT/chip ISA Rust change, or a GR block with no clean equivalent. Even then: SKIP and CONTINUE.

## Guardrails
- Never search outside /home/system/placekyt; never find / or any root scan. Never use any tree but /home/system/placekyt.
- "Builds" != "computes": always run on simKYT and compare to real GNU Radio. res.ok is not a pass.
- Production quality — these ship in a customer modem. No test-friendly shortcuts; tolerances are derived, never tuned to pass.
- Keep docs + manifest in sync as you go.

## Loop pacing & stop
Self-pace: one block fully (1-9), commit, continue. After the last planned block is done-or-blocked, STOP (no reschedule) and present: blocks landed (done) with commit hashes, blocks blocked with the exact root cause, and the full-suite result. Do not ask anything mid-run — you have everything; use the manifest and your judgment.
```

---

## What changed from v1 (why the last run under-performed)
- **Continuation made non-negotiable** with a dedicated "DO NOT GIVE UP" section — last run stopped at the first wall (IIR) instead of skip/log/continue, leaving the easy firdes filters unbuilt.
- **"Before you call it impossible" rule** + the worked half-and-double-MSUQ example — the IIR was wrongly declared impossible; it was buildable (and the real bug was a silent clamp).
- **Complex harness now exists** — told the agent to use it (last run correctly identified it was missing and stopped; that blocker is removed).
- **Manifest-is-truth + re-read from disk** — guards against a stale local manifest (a pull was missed last time).
