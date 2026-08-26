<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Robust RX — coarse frequency recovery (and the chain that dies without it)

**The story this demo tells:** real radios never see a perfectly-tuned carrier.
This burst arrives with a **0.18 cycles/sample frequency offset** — half again
beyond what a Costas loop can pull in — and TWO receivers on the same chip try
to demodulate it:

```
'rx'  : FLLBandEdge(2 sps, 0.35, 17 taps, bw 0.1) ─▶ Costas(0.05, order 2) ─▶ BPSK slicer   BER 0
'ctl' : ──────────────────────── the same Costas(0.05, order 2) ─▶ BPSK slicer   BER ~0.2 (garbage)
```

The **band-edge FLL** (GNU Radio's `digital.fll_band_edge_cc`) is the coarse
frequency-recovery stage of the industry receiver cascade: it measures the
signal's band-edge power imbalance and strips the bulk of the offset, leaving a
residual far inside the downstream Costas capture range. The second chain is
the classic coherent receiver's carrier-recovery core **without** the FLL —
the on-screen negative control. Its scope shows churning garbage bits while
the FLL-fronted chain's scope shows the clean recovered bit stream: *the old
chain dies, this one locks*.

The chain topology, parameters, and operating point are the FLL block's own
end-to-end chain gate **verbatim** (`verification/tests/test_fll_band_edge.py`
tier 5, which also proves the same competence claim on live GNU Radio at GR's
own operating point — its float Costas breaks by foff≈0.03, its fll+costas
chain recovers BER 0 at 0.05). The stimulus is full raised-cosine (Nyquist)
shaped BPSK, so the symbol instants are ISI-free without a matched filter and
the demo isolates the carrier-recovery story. The chip's Q15 Costas is a
stronger loop than GR's float one (pull-in ≈0.12 alone), which is why the
on-chip break point — and this demo — sits at 0.18.

Two streams share one placeKYT array (each = one injection landing + one
tagged egress off the shared duplex ports). Note the display convention: a
complex-input chain's sink emits **raw word floats** (the receiver
convention — a sliced bit lives in the word LSB), so the 0/1 bits plot
directly with no rescale, and both display sinks loop their genuine one-batch
result (`server_repeat=True` — a QT time sink strands the tail of a finite
stream; the gate asserts the repetition is a clean copy).

## What is verified

`verification/tests/test_robust_rx_example.py` (6 tests) on real simKYT via
the real pipeline (import → generic auto-P&R → build), plus the shipped
artifacts:

- The `.grc` imports, places, routes (no corridor transits a chip-port cell)
  and builds.
- **BER 0** through the placed FLL→Costas→slicer chain at foff = 0.18.
- **The negative control CAN fail and does** (INV-4): the same burst into the
  Costas-only chain measures BER ≈ 0.17 — the phase/lag/polarity decision
  search cannot rescue an unlocked chain.
- **Shipped-`.kyt` verdicts**: driving the committed artifact (not a
  reconstruction) reproduces both.
- **The old receiver really dies**: the shipped `coherent_bpsk_rx.kyt` (RRC
  MF → Costas → Gardner → slicer) fed this same burst fails (BER > 0.05).
- The stimulus is pinned to the chain gate's class (600 syms, 2 sps, RC 0.35,
  foff 0.18, seed 5; spectral centroid ≈ foff).

`verification/tests/test_examples_grc_userpath.py::test_robust_rx_shipped_grc_user_path`
hosts the SHIPPED `.kyt` exactly as the GUI's *Run as GNURadio Server* and
runs the SHIPPED `.grc` GRC-generated under the real GNU Radio interpreter:
BER 0 / control-fails verdicts plus clean `server_repeat` repetition on both
display sinks.
`placekyt/tests/test_gr_client_loop_examples.py::test_robust_rx_real_gr_client_duplex`
drives the same verdicts through the genuine `kyttar.source`/`kyttar.sink`
DuplexRendezvous client stack.

```
$ python examples/robust_rx/robust_rx_demo.py
   'rx'  recovered 1200/1200 bit words, BER 0.0
   'ctl' recovered 1200/1200 bit words, BER 0.1733  (the negative control MUST fail)
RESULT: LOCKED — FLL chain BER 0 at foff=0.18; Costas-only chain fails (negative control)
```

81/120 cells, 5 blocks (the FLL is a compact 7×4 serpentine fold — 22 cells
with no walled-off interior, plus its route corridor cells). Not verified: the literal Qt windows (the recovered
data paths, including what each scope is fed, are gate-covered end to end).
The FLL does not bring its internal frequency-estimate tap out to GRC (only
the corrected complex stream), so the demo shows the *effect* of convergence
— clean bits after the ~150-symbol acquisition — not the estimate itself.

Pacing note: per-sample (`pipelined: 'no'`). The FLL is a fully-serial
21-cell serpentine loop (saturation-proven bespoke in its block gate at ~2500
events/sample); per-sample is exact on every layout.

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/robust_rx/robust_rx.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/robust_rx/robust_rx.grc
```

(Or, after pressing **Generate** in GRC once, run the generated top-block directly: `python3 examples/robust_rx/robust_rx.py`. That file is build output — it is not checked in, and GRC recreates it from the `.grc`.)

| File | What |
|------|------|
| `robust_rx.grc` | GRC-first source (kyttar markers; the offset burst + both chains + scopes). |
| `robust_rx.kyt` | Auto-generated placed+routed project (import → auto-P&R → save). |
| `robust_rx.py` | GRC-generated top block. |
| `build_kyt.py` | Regenerates the `.kyt`. |
| `robust_rx_demo.py` | Headless END-TO-END demo — both chains, BER verdicts, one command. |
