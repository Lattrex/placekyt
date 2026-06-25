<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Block verification — per-block lessons log

Append-only, newest first. One entry per block as it is verified: what was tried,
what passed/failed, the derived tolerance, and any block-specific gotcha. Promote
anything that generalizes across block classes into `invariants.md`.

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
