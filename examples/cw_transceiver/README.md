<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# CW (Morse) FULL TRANSCEIVER — TX + RX duplex, ONE shared SRAM panel

One chip simultaneously **keys outgoing ASCII text** as ITU-R M.1677-1 Morse
(bit-exact vs the golden) and **decodes incoming keyed audio** back to
characters — the keyer's per-character Morse ROM and the decoder's
reverse-Morse LUT sharing the single SRAM panel, shipped inside the `.kyt`
like a bitstream.

```
TX ('tx'):  chars ─▶ CWKeyer (SRAM ROM, completion flow control) ─▶ ITU-R keyed envelope
RX ('rx'):  keyed audio ─▶ Abs (envelope det.) ─▶ STREAMING CWDecoder (SRAM LUT @16384) ─▶ chars
```

## The TX half: the flow-controlled record chain (why the "NQ" bug can't recur)

Feed ASCII bytes at runtime — any message, no rebuild. The panel holds a
**message-independent Morse ROM** (one run-record region per ASCII code
point). An earlier revision sequenced records by host triggers, and under
load a record's push-read could overwrite the player's registers mid-play
(observed in the GUI as `CQ…` keyed as `NQ…`). The current chain closes that
**by construction**:

- One injected character makes the **fetch cell** point the panel at that
  character's ROM region (`address = byte << 7`) and stream its first record.
- The **player** plays the record's `count` samples, then sends a
  **completion kick** through the crossover's control track back to the
  fetch's `next` entry — the next record physically cannot be fetched until
  the current one finishes. The sparse panel's unwritten words read 0, so
  every region ends in an implicit `(0,0,0)` END record: the player halts and
  the chain idles awaiting the next character.
- Enabler: the panel's **read auto-increment** (`SramPanel.auto_inc_read`) —
  the fetch never rewrites the address register per word.

A SPACE keys the 7-dot-unit inter-word gap (its own ROM region); `wpm`
(PARIS: `dot_ms = 1200/wpm`) and `samples_per_dot` set the timing as **sample
counts** in the ROM records — the async fabric never times anything.

## The RX half: the streaming fixed-unit decoder

The CWDecoderBlock's fixed-unit mode (`unit_samples` > 0): a 4-cell skimmer
locked to the keyer's configured unit (`samples_per_dot == unit_samples`) —
detect (run thresholding) → classify (dot/dash + char boundary) → the
embedded SramController's `lookup` (every read carries its OWN R3/R4
descriptors — the shared-panel-safe protocol) → panel push-read → emit.

**Documented v1 streaming limits** (honest, gated as such):
- word gaps decode as character boundaries only — NO SPACES (the space branch
  does not fit the classify cell's 32-word budget); compare letters.
- an RX burst must end with an **EOT blip** (≥ 2 units of silence then ≥ 1 ON
  sample): it flushes the final character's gap and is itself never decoded.
  The shipped `.grc`'s `rx_sig` stimulus (a real keyed envelope for
  `'RST 599 73'`) ends with one.
- per-sample paced only (the standard panel contract; the server enforces it
  for panel-backed designs — `SimServer.force_per_sample`).

**Duplex geometry** (the kicker-form template branch in
`engine/panel_pnr.py`): the TX crossover keeps its completion `track_c` (the
keyer's flow control), so the RX egress rides free cells onto its own
crossing cell (colxo) and exits with the RX stream's tag. Corridor taps and
the panel return fork are standard build BROKERS (plain routing cells), the
`x1_out` port cell is a plain routing cell both clients' panel words
traverse, and no corridor routes through a DSP block's cells
(`verification/tests/test_kyt_route_transits.py`).

## What is verified

```
$ python examples/cw_transceiver/cw_transceiver_demo.py
   TX: 1224/1224 envelope samples, bit-exact vs the ITU-R golden: True
   RX: decoded 'RST59973' (letters of 'RST 599 73'), exact: True
RESULT: EXACT — full duplex CW transceiver, TX == golden AND RX == sent letters
```

- `verification/tests/test_cw_transceiver_example.py` (6): TX BIT-EXACT vs
  the keyer golden while RX runs interleaved; RX == the sent letters == the
  streaming golden; ALL 36 ITU-R alphanumerics round-trip through the shared
  LUT; shipped-`.kyt` parity; a corrupted LUT word corrupts exactly that
  character (mutation).
- `placekyt/tests/test_gr_client_loop_examples.py::
  test_cw_transceiver_real_gr_client_duplex` — the same exactness through the
  genuine GR client duplex loop (real `kyttar.source`/`sink` over the socket
  to the hosted server).
- `verification/tests/test_examples_grc_userpath.py` — the **user path**: the
  SHIPPED `.grc` is GRC-generated and run against the SHIPPED `.kyt` hosted
  on port 58950; TX must be bit-exact and RX must decode `RST59973` from the
  flowgraph's own embedded stimulus.
- GUI-edit safety: routes start/end ON block cells, the panel return port is
  routable, and `refresh_panel_params` re-derives every placement-dependent
  parameter from the current routes at build
  (`verification/tests/test_panel_param_refresh.py`).

70/120 cells, 5 blocks (+ broker routing cells), 3499-word shared panel image.

## The GRC window (what you should SEE)

Run the `.kyt` as GNURadio Server, then Run the `.grc`: the **TX scope**
fills with the keyed ITU-R envelope as it streams, and the **"RX decoded
chars"** scope draws the 8 ASCII codes (82 'R', 83 'S', 84 'T', 53/57/57
'599', 55/51 '73') via the `rx_chars` ×32768 rescale (the kyttar sink emits
q15/32768 floats). The RX sink LOOPS the genuine one-batch decode for display
(`server_repeat=True`): GNU Radio's scheduler STRANDS the tail of a finite
stream, so an 8-character burst can NEVER paint a QT time sink on its own —
the window stayed blank through two audit rounds while the decode was
verifiably arriving (the userpath gate asserts the text AND that the loop is
a clean repetition; pixel-proven). Per-sample pacing means the
decode lands near the END of the run — wait for the burst to finish.

## Known limits

- Sending a new character mid-character resets the ROM region (the keying
  truncates) — characters are paced, matching the physical CW regime.
- The literal Qt window (rendering/interaction) is not driven by tests; the
  full data path is.
- **Re-run `cd gr-kyttar && ./install.sh` (it prompts for sudo)** after pulling: GRC compiles
  against the system-installed block ymls/markers, which shadow the repo.
- On-chip edge cap is `edge_samples <= 4` (the player's END-test + completion
  kick share the 32-word cell with the Hann LUT).

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/cw_transceiver/cw_transceiver.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/cw_transceiver/cw_transceiver.grc
```

(Or, after pressing **Generate** in GRC once, run the generated top-block directly: `python3 examples/cw_transceiver/cw_transceiver.py`. That file is build output — it is not checked in, and GRC recreates it from the `.grc`.)

## Files

| File | What |
|------|------|
| `cw_transceiver.grc` / `.kyt` | The full transceiver (open the `.kyt`, Run as GNURadio Server, then run the `.grc`). |
| `build_transceiver_kyt.py` | Regenerates `cw_transceiver.kyt` via import → duplex auto-P&R → build. |
| `cw_transceiver_demo.py` | Headless END-TO-END duplex demo/proof. |
