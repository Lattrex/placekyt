<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# PSK31 FULL TRANSCEIVER — TX + RX duplex on one array, ONE shared SRAM panel

The first two-client shared-panel design: both Varicode tables live in the
single SRAM panel, and the encoder *and* decoder talk to it through the chip's
one x1 port pair — while the TX and RX streams duplex through the shared x16
ports.

```
TX ('tx'):  chars ─▶ VaricodeEncoder (SRAM, table @ addr_base 1024) ─▶ DiffEncoder
            ─▶ BPSK mapper ─▶ hold ×8 ─▶ raised-cosine envelope ─▶ shaped baseband
RX ('rx'):  soft symbols ─▶ BPSKSlicer ─▶ DiffDecoder
            ─▶ VaricodeDecoder (SRAM, reverse map @ 1..955) ─▶ ASCII chars
```

## How one panel serves two clients

- **Disjoint address regions**: the encoder's embedded SramController adds
  `addr_base` (1024) to every lookup key; the decoder's reverse map occupies
  1..955. The importer's shared-panel synthesis **refuses overlapping
  regions** with a named error (gated).
- **Per-read descriptors**: every panel read writes its OWN R3/R4 push-read
  descriptors (the SramController `read` protocol), so the two clients'
  lookups interleave safely — each push returns to ITS consumer cell.
- **The duplex corridor template** (`engine/panel_pnr.py`): the proven TX
  template plus an RX half threaded around it — the RX input rides *through*
  the TX crossover to a tap, the RX egress crosses the return corridor and
  exits via the TX crossover's new **data track_c** (dest_c = the RX stream
  tag), and every relay cell that others transit **restores its transit face**
  (the standard broker self-restore behavior, now on CrossoverBlock).

PER-SAMPLE PACED (the standard panel contract; the GRC server enforces it for
panel designs — `SimServer.force_per_sample`).

## The GRC window (what you should SEE)

The **TX scope** shows the raised-cosine-shaped PSK31 baseband. The **"RX
decoded chars"** scope draws the 8 ASCII codes — `'R 599 73'` — through the
`rx_chars` ×32768 rescale (the kyttar sink emits q15/32768 floats). The RX
sink LOOPS the genuine one-batch decode for display (`server_repeat=True`):
GNU Radio strands the tail of a finite stream, so an 8-char burst can never
paint a QT time sink on its own (pixel-proven fix; the userpath
gate asserts the text and clean repetition). The decode lands near the END
of the (per-sample paced) run.

**The RX stimulus is honest but pre-demodulated:** the `.grc`'s `rx_sig` is
the diff-encoded ±0.9 soft-symbol stream of the Varicode bits for
`'R 599 73'` — exactly what a coherent PSK31 demodulator hands the slicer at
symbol rate. There is NO carrier/timing recovery in this RX path; the
coherent front end is the separately-proven `coherent_bpsk_rx` spine.

## What is verified

```
$ python examples/psk31_transceiver/psk31_transceiver_demo.py
   TX: 1032/1032 baseband samples, sample-exact vs golden: True
   RX: decoded 'OK 599 TU 73', exact: True
RESULT: EXACT — full duplex transceiver, TX == golden AND RX == sent text
```

`verification/tests/test_psk31_transceiver_example.py` (6 tests): TX
**SAMPLE-EXACT** vs the psk31 golden (`psk31_tx_golden.py`, which is
proven against) while RX runs interleaved; RX decodes the sent text exactly;
EVERY printable ASCII char round-trips through the shared reverse map;
shipped-`.kyt` parity; the addr_base-overlap refusal; and a **mutation** (one
corrupted reverse-map word must corrupt exactly that character's decode).

`placekyt/tests/test_gr_client_loop_examples.py::test_psk31_transceiver_real_gr_client_duplex`
runs the **genuine GR client** — two real `kyttar.source`/`kyttar.sink` pairs
through the DuplexRendezvous against the hosted `.kyt` — and holds the same
exactness (the GUI Run data path minus the literal Qt window).

69/120 cells, 9 blocks, 256-word panel image (both tables). The RX drive in
the demo is the diff-encoded ±0.9 symbol stream of the golden Varicode bits —
what a coherent PSK31 demodulator hands the slicer at symbol rate (the
coherent front end itself is the proven `coherent_bpsk_rx` spine).

## Run it

Two terminals, two commands — run both **from the repo root** (`placekyt/`).

**1. Host the chip** (terminal 1):

```bash
.venv/bin/python placekyt/main.py examples/psk31_transceiver/psk31_transceiver.kyt
```

Then **Simulation → Run as GNURadio Server** (port **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/psk31_transceiver/psk31_transceiver.grc
```

(Or, after pressing **Generate** in GRC once, run the generated top-block directly: `python3 examples/psk31_transceiver/psk31_transceiver.py`. That file is build output — it is not checked in, and GRC recreates it from the `.grc`.)

| File | What |
|------|------|
| `psk31_transceiver.grc` | GRC-first source (two tagged streams, paced). |
| `psk31_transceiver.kyt` | Auto-generated placed+routed project (open in placeKYT, Run as GNURadio Server). |
| `build_kyt.py` | Regenerates the `.kyt`. |
| `psk31_transceiver_demo.py` | Headless END-TO-END duplex demo. |
