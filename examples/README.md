<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# placeKYT examples

Each folder here is a self-contained demo: a GNU Radio flowgraph (`.grc`), often a
pre-built placeKYT design (`.kyt`), plus any helper scripts and its own README with
a walkthrough. They all run the same way — you **host the chip in placeKYT** and
**drive it from GNU Radio Companion** — so once you've done one, you've done them
all.

New to placeKYT? Start with **[`gain/`](gain/)**, then the flagship
**[`bpsk_modem/`](bpsk_modem/)**. The full setup-to-demo walkthrough
(installing GNU Radio, the Kyttar blocks, and the two-terminal run) is in
**[`../INSTALL.md`](../INSTALL.md)**.

Every demo's README is **click-and-run**: it gives you the two exact commands
(host the chip, drive it) as copy-paste code blocks. Open a README, copy the
first command into a terminal, copy the second into another, and you're running.

## The demos

| Demo | What it is | Blocks | Open |
|------|------------|:------:|------|
| **[gain/](gain/)** | **Start here.** The simplest possible design — one gain block (multiply by a constant). The best place to learn the placeKYT UI and the GNU Radio ↔ placeKYT workflow end to end. | 1 | `.kyt` or `.grc` |
| **[gain_hw/](gain_hw/)** | The **hardware** counterpart to `gain/` — the same one-block design driven by a continuous live sine on the real board (streaming mode) instead of a bounded sim burst. | 2 | `.kyt` |
| **[gain_2p2s/](gain_2p2s/)** | The first **multi-chip** example: four independent gain streams, one per chip, multiplexed across the 2P2S dev board's two parallel daisy-chains. | 4 (4 chips) | `.kyt` |
| **[fft128_2p2s/](fft128_2p2s/)** | The same 128-point FFT **retargeted onto the real 2P2S dev board** — die 0 on chain A's head, die 1 on its tail, joined by the board's own on-carrier series link, with chain B wired and idle. **200/200 samples bit-exact** and **DRC-clean against `dev2p2s.kdb` itself**. | 2 on a 4-die board | `.kyt` or `.grc` |
| **[bpsk_modem/](bpsk_modem/)** | **The flagship.** A full-duplex BPSK **modem** — a transmit chain and a coherent receive chain (RRC matched filter → Costas → Gardner → BPSK slicer) sharing one chip, demuxed by `stream_id`. The full digital link on a single Kyttar array; the one to study first. | 8 (TX+RX) | `.grc` or `.kyt` |
| **[coherent_bpsk_rx/](coherent_bpsk_rx/)** | The coherent BPSK **receiver** on its own — RRC matched filter → Costas carrier recovery → Gardner timing recovery → BPSK slicer. The input carries a carrier **and** a timing offset; the chip recovers the bits at **BER 0**. An extra, receiver-only view of the same recovery chain the modem uses. Includes a headless `batch_check.py`. | 4 | `.grc` or `.kyt` |
| **[qpsk_modem/](qpsk_modem/)** | A full-duplex **QPSK** modem — a transmit (`PSKSymbolMapper → ComplexUpsampler → RRC → IQUpconvert`) and a coherent receive (`RRC matched filter → order-4 Costas → M&M timing recovery → QPSK slicer`) chain sharing one chip. Fully complex I/Q, **2 bits per symbol**, recovers the symbols at **BER 0** through a carrier and a timing offset. Ships a headless `batch_check.py`. | 8 (TX+RX) | `.grc` or `.kyt` |
| **[fsk4_modem/](fsk4_modem/)** | A full-duplex **4FSK** modem — a transmit (`FSK4SymbolMapper → Upsampler → RRC → FrequencyModulator`) and a receive (`QuadratureDemod → RRC matched filter → FSK4SyncTimingRecovery → FSK4Slicer`) chain sharing one chip. **2 bits/symbol** as four frequency-deviation levels; timing is recovered by cross-correlating a known sync word, recovers the dibits at **BER 0**. Ships a headless `batch_check.py`. Hand-placed: **open the `.kyt` directly, don't import the `.grc`** (see its README). | 8 (TX+RX) | `.kyt` only |
| **[qam16_modem/](qam16_modem/)** | A full-duplex **16-QAM** modem — a transmit (`QAM16SymbolMapper → ComplexUpsampler → RRC → IQUpconvert`) and a coherent receive (`RRC matched filter → ComplexGain → Mueller & Müller timing recovery → decision-directed 16-QAM Costas → 16-QAM slicer`) chain sharing one chip. **4 bits/symbol** on the square 16-point `digital.constellation_16qam()` grid, recovers the symbols at **BER 0** through a carrier offset. Ships a headless `batch_check.py`. Hand-placed: **open the `.kyt`, don't import the `.grc`** (see its README). | 9 (TX+RX) | `.kyt` only |
| **[am_transceiver/](am_transceiver/)** | A double-sideband **AM** transceiver: a coherent product modulator and detector sharing one chip. The simplest analog transceiver. | 4 (TX+RX) | `.grc` or `.kyt` |
| **[fm_transceiver/](fm_transceiver/)** | An **FM** transceiver: a VCO modulator (`FrequencyModulator`) and a quadrature discriminator (`QuadratureDemod`) sharing one chip. | 2 (TX+RX) | `.grc` or `.kyt` |
| **[ssb_weaver/](ssb_weaver/)** | A single-sideband **SSB** transceiver built the Weaver (third-method) way, using the complex-FIR filter blocks. The most involved analog demo — **hand-placed: open the `.kyt` directly, don't import the `.grc`** (see its README). | 7 (TX+RX) | `.kyt` only |
| **[cw_transceiver/](cw_transceiver/)** | A full **CW (Morse) transceiver** — an SRAM-backed keyer (ASCII in → ITU-R keyed envelope out) and a streaming fixed-unit decoder (keyed audio in → ASCII out) sharing ONE SRAM panel on one chip. The first table-driven (memory-tier) transceiver. Per-sample paced (panel contract). | 5 + panel | `.grc` or `.kyt` |
| **[psk31_transceiver/](psk31_transceiver/)** | A full **PSK31 transceiver** — Varicode encoder → DBPSK mapper → raised-cosine shaping on TX, slicer → diff decoder → Varicode decoder on RX, BOTH Varicode tables sharing ONE SRAM panel. Per-sample paced (panel contract). | 9 + panel | `.grc` or `.kyt` |
| **[data_link/](data_link/)** | A **scrambled byte data link** — an 11-stage bit-processing loopback (unpack → NOT/AND/map → LFSR scramble → differential encode/decode → descramble → pack) that returns every payload byte EXACTLY. The whole-chain proof for the byte/FEC/digital blocks no modem touches; runs Full-speed (saturated) with a dedicated saturated gate. | 11 | `.grc` or `.kyt` |
| **[tmr_pipeline/](tmr_pipeline/)** | **Triple-modular redundancy** next to an ordinary stream on ONE array — a byte ramp fans out to three identity workers with a depth-neutral fault injector on every arm (constants `0/f/0`), and the **TMR voter** (arrival-face rotation) emits `[value, status]` packets: with a 1-LSB path-B fault every packet is `[ramp byte, 2]` — the value **still correct** (TMR corrects), the status naming path B — while a single-path 0.5× gain stream runs co-resident. Redundancy is an AREA cost, not a performance cost, chosen per path at P&R time. Hand-placed: **open the `.kyt` directly, don't import** (see its README). | 9 | `.kyt` |
| **[lz4_stream/](lz4_stream/)** | **LZ4 compression as a variable-rate stream** — the SRAM-backed `LZ4EncoderBlock` and `LZ4DecoderBlock` (TWO panel-backed blocks, the design limit) share one chip and its single panel port pair. A 1 KB payload that switches character mid-stream (512 repetitive + 512 random bytes) compresses to **540 bytes (52.7%)** — the repetitive half ~16:1, the random half ~1:1 — and round-trips **byte-exactly** through the on-chip decoder. The output rate is a function of the DATA, which a fixed-rate DSP pipeline cannot do; the encoder-output scope shows it against the worst-case (incompressible-bound) buffer. Per-sample paced (panel contract). Hand-placed: **open the `.kyt` directly, don't import** (see its README). | 2 + panel | `.kyt` |
| **[fec_link/](fec_link/)** | The **FEC protocol link** — a channel burst story on one array: message bytes → Hamming(7,4) encoder → 4×3 block **interleaver** on TX (plus an on-chip **CRC-16** of the frame), a deterministic 2-bit channel burst, then deinterleaver → Hamming **syndrome decoder** → the exact message back, proven by the **CRC frame verdict** (chip CRC == recomputed CRC). The same burst without the interleaver kills a codeword and fails the CRC — the gate proves that control on-chip too. | 7 | `.grc` or `.kyt` |
| **[audio_effects/](audio_effects/)** | Three placed **audio effects** (echo, NCO tremolo, feedforward comb) — each a dataflow JOIN (two arms reconverging on a combiner), the topology that exercises the counting-join machinery. Per-sample paced (documented cross-block join-skew limit). | 3–6 each | `.grc` or `.kyt` |
| **[audio_meter/](audio_meter/)** | An **audio tail + S-meter + true RMS**: DC blocker → AGC → band-reject → squelch on one stream, envelope → moving average → 10·log10 meter on a second, a true-RMS level row (`rms_ff`) on a third, duplexed on one array. Runs Full-speed (saturated) with a dedicated saturated gate. | 8 | `.grc` or `.kyt` |
| **[robust_rx/](robust_rx/)** | **Coarse frequency recovery** — a BPSK burst with a 0.18 cyc/sample carrier offset (beyond Costas pull-in) into TWO receivers on one chip: **FLL band-edge → Costas → slicer** recovers **BER 0**, while the Costas-only chain (the classic coherent core, the on-screen negative control) churns garbage. Real-world offsets: the old chain dies, this one locks. | 5 | `.grc` or `.kyt` |
| **[complex_math/](complex_math/)** | **Two-stream complex arithmetic** — two analytic tones into **AddCC / SubCC / MultiplyCC** on one chip: superposition, difference, and THE MIXER (multiplying analytic tones adds their frequencies — one clean tone at f_a+f_b, asserted bin-sharp). Bit-exact vs the blocks' references; the wiring pattern to copy for any two-complex-stream GRC design. | 3 | `.grc` or `.kyt` |
| **[channel_selector/](channel_selector/)** | A **channel selector**: FreqXlatingFIR channelizer → complex low-pass → rotator → conjugate → imag rail. Per-sample paced (the FreqXlatingFIR is saturation-bespoke; see its README). | 6 | `.grc` or `.kyt` |
| **[lms_equalizer/](lms_equalizer/)** | The **adaptive-equalizer constellation snap** — multipath-smeared QPSK through the on-chip decision-directed **LMS equalizer**: the constellation cloud snaps onto the four clean points *within the burst* (the adaptation runs on the array, per sample). Converged tail **BER 0**. | 1 | `.grc` or `.kyt` |
| **[css_transceiver/](css_transceiver/)** | A **chirp-spread-spectrum receiver** — the whole CSS receive spine on one array: **dechirp** (conjugate chirp mixer) → **16-point streaming FFT** → bin power → the alignment **Delay(1)** → **framewise argmax**. The winning bin *is* the symbol (after the `brev4` bit-reversal FFT16's output order requires). One continuous burst carries `KYTTAR CSS` twice: at **+10 dB it decodes exactly (SER 0)**, at **−10 dB the same chain collapses** — the negative control runs **on the chip**. RX-only: the transmitter and channel are host-side numpy, bit-exact to the TX blocks' own references (see its README). | 5 (RX) | `.kyt` |
| **[cordic_polar/](cordic_polar/)** | **CORDIC polar decomposition** — one AM'd rotating phasor split into the two CORDIC vectoring chains (magnitude → the AM envelope, atan2 → the phase sawtooth), each overlaid on the stock GNU Radio reference block. Open the `.kyt` directly (dense guided-anchor placement; a fresh import doesn't route cleanly). | 2 | `.kyt` |
| **[fft_spectrum/](fft_spectrum/)** | A **live spectrum analyzer on the fabric** — a **streaming FFT** and its per-bin power stage both placed on chip, so what leaves `x16_out` is already a power word per frequency bin; the flowgraph un-reverses the FFT's bit-reversed (DIF) bin order and plots it. A tone at bin 11 leaves the chip on slot 52 and lands at **bin 11, −0.9 dBFS, every other bin at the −90 dBFS floor**. Two sizes ship: **N=64** (104/120 cells) and **N=32** (80/120). Open the `.kyt` directly — the FFT is a CHIP_SCALE block the auto-packer cannot place. | 2 | `.kyt` |
| **[gru_classifier/](gru_classifier/)** | **Machine learning on the array** — a **GRU modulation classifier**: complex baseband in, a class word out every 32 samples (SSB / BPSK / 4-FSK / noise). Both features (RMS and zero-crossing rate) are library DSP blocks, and the recurrent network itself — gates, activation tables, hidden state and the 4-class readout — is one placed block whose weights come from the trained model in `ml/`. The whole chain, inference included, runs on one array at **102/120 cells**. Open the `.kyt` directly. | 7 | `.kyt` |

**Open `.kyt`** — the demo ships a pre-placed, pre-routed design you can open
directly (**File → Open**) and explore on the canvas without importing anything.
**Open `.grc`** — you import the flowgraph (**File → Import GNURadio
Flowgraph…**) and placeKYT auto-places and routes it. Either way, you then **Run
as GNURadio Server** and drive it from `gnuradio-companion`.

> Seven demos are the exception — the **SSB Weaver**, the **FSK4 modem**, the
> **16-QAM modem**, the **CORDIC polar** demo, the **CSS receiver**, the
> **GRU classifier**, and the **FFT spectrum** analyzer: you **must open their
> `.kyt` directly** (importing the `.grc` can leave nets unrouted, or produce a
> layout that builds without computing correctly). For the first six it is
> placement density and pinned geometry. For
> `fft_spectrum` it is a hard limit rather than a tuning problem: its FFT is a
> **CHIP_SCALE** block whose verified layout is a full-height ctl/out spine, and
> the generic auto-packer has no model for that class — it shifts the spine off
> the array and the import fails outright. Each README explains why. Every other
> demo places and routes from the `.grc`.

## The common workflow (every demo)

Every demo runs the same two-terminal way. Run both commands **from the repo
root** (`placekyt/`), with the venv already set up (see [`../INSTALL.md`](../INSTALL.md)).
Each demo's README repeats these two commands filled in for that demo — so you can
copy-paste straight from there.

**1. Host the chip** (terminal 1) — launch placeKYT. For a demo that ships a `.kyt`,
pass it and placeKYT opens it directly; otherwise launch placeKYT and **File →
Import GNURadio Flowgraph…** the demo's `.grc`:

```bash
.venv/bin/python placekyt/main.py examples/gain/gain.kyt
```

Then in placeKYT: **Simulation → Run as GNURadio Server** — the status bar shows
the bound port (default **58950**). Leave placeKYT running.

**2. Drive it** (terminal 2) — open the flowgraph in GNU Radio Companion and press
**▶ Run** (F6):

```bash
gnuradio-companion examples/gain/gain.grc
```

A plot window opens showing the demo's input against the output coming back from
the placeKYT-hosted chip.

The DSP always runs **on the chip inside placeKYT**; the GNU Radio blocks are the
front-end that streams stimulus in and plots the result. To change a design, edit
it in placeKYT (or in the flowgraph and re-import) and re-host.

## Watch the data flow — the cell-animation button

placeKYT can **animate the chip as it runs**, so you can literally see data move
through the cell array. On the **Simulation toolbar**, tick **Enable cell
animation**; then run — from the in-tool stimulus or from a GNU Radio drive. Cells
**glow green as they execute**, and per-word arrows show each value hopping
cell-to-cell along its route toward the output port. It's the clearest way to *see*
what a design is doing: where the signal enters, which cells compute, how it snakes
to the egress port. The **Speed** slider beside the checkbox paces it — the chip
steps in lockstep with the animation, so a stall or a dead route is visible as it
happens.

It's **off by default** — leaving it off runs flat-out with no visual overhead, and
the slider is greyed. Turn it on when you want to understand or debug a layout; turn
it off for a fast run. Worth trying from the very first demo (`gain/`): one cell
lighting up as each sample passes through makes the whole host-and-drive model click.

> On **Run**, GNU Radio may pop up a harmless *"x-terminal-emulator is missing"*
> warning — close it and the flowgraph runs normally. See
> [`../INSTALL.md`](../INSTALL.md) for the one-line way to silence it.

## The transceiver pattern

The duplex demos share one structure: a **transmit** chain and a **receive**
chain live on the *same* chip, kept separate by a `stream_id` tag (`"tx"` / `"rx"`)
on the source and sink blocks. That is how a single Kyttar array hosts a
full-duplex link. Each duplex demo's README draws the TX and RX signal path
and names the exact GNU Radio block each Kyttar block is equivalent to.

## Regenerating a flowgraph (advanced)

The AM, FM, and SSB demos include a `gen_grc.py` that regenerates the `.grc` from a
script — the DSP parameters (sample rate, carrier, filter cutoffs) live at the top
of that file. Edit them and re-run it to rebuild the flowgraph; it comes out with
script-default block positions, so re-arrange to taste in `gnuradio-companion`. The
`.grc` files checked in here are already laid out for readability, so you only need
this if you want to change the signal parameters.

---

> Building or verifying your **own** block (rather than using one)? The gain block
> is also the reference for that workflow — see
> [`../verification/examples/gain_reference/`](../verification/examples/gain_reference/)
> and [`../BLOCK_AUTHORING_GUIDE.md`](../BLOCK_AUTHORING_GUIDE.md). These examples
> *use* blocks; those show how to *make* them.
