<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Installing placeKYT

This guide takes you from a **clean Ubuntu install** to the **running coherent
BPSK receiver demo in under 5 minutes**, with every command spelled out. No prior
GNU Radio or cmake knowledge needed.

placeKYT has three pieces that install together:

1. **placeKYT** — the Qt IDE + CLI + place/route/build engine (pure Python).
2. **`gr_kyttar`** — the block-build library: placement + bitstream generation (pure Python).
3. **simKYT** — the simulator, a compiled extension importable as `simkyt` (ships prebuilt).

> **How the two halves connect (read this — it explains the demo).** placeKYT runs
> in its own Python 3.12 environment and owns *everything* about the chip: it
> places the DSP blocks on the cell array, routes them, builds the bitstream, and
> runs the simulator. GNU Radio is a **separate** program. The Kyttar GNU Radio
> blocks are thin front-ends that talk to a placeKYT-hosted chip over a local TCP
> socket — they carry no simulator code, so they load under whatever Python your
> GNU Radio uses. You design the chip in placeKYT; you drive it (stimulus +
> plots) from a GNU Radio flowgraph.

For this release, install from source on **Linux (x86-64) with Python 3.12**.
Other platforms and one-file installers are on the roadmap (see §6).

---

## 1. System packages (one apt command)

On a fresh Ubuntu 24.04 box, install everything the OS side needs:

```bash
sudo apt update
sudo apt install -y git python3.12-venv gnuradio libxcb-cursor0 xterm
```

What each is for:

| Package | Why |
|---------|-----|
| `git` | clone the repo |
| `python3.12-venv` | create the placeKYT virtual environment (placeKYT requires Python 3.12) |
| `gnuradio` | GNU Radio 3.10+ and `gnuradio-companion` for the flowgraph demo *(skip if you only want the placeKYT GUI/CLI)* |
| `libxcb-cursor0` | required by Qt 6.5+ for the placeKYT GUI; without it the GUI aborts with *"Could not load the Qt platform plugin xcb"* |

Confirm Python:

```bash
python3 --version      # must report 3.12.x
```

---

## 2. Install placeKYT (the venv side)

```bash
git clone https://github.com/Lattrex/placekyt.git
cd placekyt

# A project-local virtual environment (Python 3.12)
python3 -m venv .venv

# placeKYT's Python dependencies (PySide6, ruamel.yaml, ortools, pytest, ...)
.venv/bin/pip install -r placekyt/requirements-dev.txt

# gr_kyttar (block-build library) + the prebuilt simkyt extension.
# Both live under runtime/python/, so one editable install wires up both
# (the simkyt .so ships prebuilt — no Rust toolchain or build step needed):
.venv/bin/pip install -e runtime/python
```

### Verify (headless, ~5 seconds)

```bash
.venv/bin/python -c "import simkyt; \
    from gr_kyttar.placement import router; \
    from gr_kyttar.bitstream.generator import BitstreamGenerator; \
    print('ok', simkyt.__version__)"
# -> ok 0.1.0
```

Build a demo design and compare it to its golden output — this exercises the full
place → route → build → simulate path:

```bash
.venv/bin/python placekyt/cli.py --test placekyt/tests/data/demo/qam16_demo.kyt \
    --chip-type placekyt/resources/chips/kyttar_10x12.yaml
# -> test PASSED: 12 output words match (tolerance 2).
```

### Launch the GUI

```bash
.venv/bin/python placekyt/main.py
```

That's placeKYT installed and working. If you don't need the GNU Radio flowgraph
workflow, you're done — design chips, build, and simulate entirely in the GUI/CLI.

---

## 3. The GNU Radio integration + the BPSK RX demo (~3 minutes)

This wires a placeKYT-hosted chip to a GNU Radio flowgraph so you can feed
stimulus and watch the receiver work. **Every Kyttar block is placeable and
wireable in GNU Radio Companion** — the blocks stream to the chip placeKYT hosts.

### 3a. Install the Kyttar GNU Radio blocks (one command)

From the repo root:

```bash
cd gr-kyttar
./install.sh
cd ..
```

`install.sh` copies the Kyttar GNU Radio module into GNU Radio's own
`site-packages/gnuradio/kyttar/` and the block definitions into GNU Radio's GRC
blocks directory, then clears the GRC cache. It backs up anything it replaces and
needs `sudo` only for system directories (it will prompt). It does **not** touch
your system Python packages or the placeKYT venv — the module it installs imports
only `gnuradio` + `numpy`. Re-run `./install.sh` any time to upgrade.

Confirm GNU Radio can see the blocks:

```bash
python3 -c "from gnuradio import kyttar; print('kyttar blocks ready')"
# -> kyttar blocks ready
```

All the demos live in one place: the top-level **`examples/`** directory. Each
subdirectory holds the flowgraph (`.grc`), the placeKYT host design (`.kyt`), and a
README. We'll use [`examples/coherent_bpsk_rx/`](examples/coherent_bpsk_rx/).

### 3b. Host the receiver chip in placeKYT

Launch the placeKYT GUI (from the repo root):

```bash
.venv/bin/python placekyt/main.py
```

Then, in placeKYT:

1. **File → Import GNURadio Flowgraph…** →
   `examples/coherent_bpsk_rx/coherent_bpsk_rx.grc`. placeKYT reads the flowgraph
   and places the receiver — RRC matched filter → Costas loop → Gardner timing
   recovery → BPSK slicer — onto the cell array, so you see the same design as a
   chip. (You start from the flowgraph, so the GNU Radio graph and the hosted chip
   are guaranteed to match.)
2. **Simulation → Run as GNURadio Server.** placeKYT builds the chip and starts
   hosting it; the status bar shows the bound port (default **58950**). Leave
   placeKYT running.

### 3c. Run the flowgraph in GNU Radio Companion

Open the same flowgraph in GNU Radio Companion (in a second terminal, leaving
placeKYT running):

```bash
gnuradio-companion examples/coherent_bpsk_rx/coherent_bpsk_rx.grc
```

You'll see the full receiver wired up: an RRC-shaped BPSK stimulus source → the
Kyttar DSP blocks (matched filter, Costas, Gardner, slicer) → QT GUI sinks — the
whole flow, visible and editable as a flowgraph. Press **▶ Run** (or F6). A window
opens plotting the **input I** (the carrier+timing-offset BPSK waveform) against
the **recovered bits** coming back from the placeKYT-hosted chip.

> **Harmless warning — just click OK.** On **Run**, GNU Radio may pop up
> *"The xterm executable 'x-terminal-emulator' is missing. You can change this
> setting in your gnuradio.conf"*. This is GNU Radio looking for a terminal
> emulator it does **not** need for this demo. **Close the dialog and the
> flowgraph runs normally** — the demo is unaffected. (The warning can persist
> even with `xterm` installed, because GNU Radio looks specifically for the
> Debian `x-terminal-emulator` alternative; it is safe to ignore. To suppress it
> entirely, add a line `xterm = /usr/bin/xterm` under `[grc]` in
> `~/.gnuradio/config.conf`.)

That's the end-to-end demo: a GNU Radio flowgraph driving a real Kyttar coherent
receiver running on the simulator inside placeKYT.

### Headless one-command check (optional)

With the server running from step 3b, you can skip the GUI flowgraph and verify
from a terminal:

```bash
.venv/bin/python examples/coherent_bpsk_rx/batch_check.py --port 58950
# streams the BPSK burst through the hosted chip and reports the recovered bits + BER
```

### Where the work happens

You **design and place** the chip in placeKYT; GNU Radio Companion lets you
**place and wire every Kyttar block** and **stream** to the hosted chip. The DSP
runs on the chip inside placeKYT — the GNU Radio blocks are the front-end. To
change the receiver, edit it in placeKYT and re-host; the flowgraph stays the
same.

---

## 4. About the simKYT extension (important)

`simkyt` is a **compiled** Python extension, so the bundled binary is specific to
**one operating system, CPU architecture, and Python minor version**. This
release ships the artifact for:

> **Linux · x86-64 · CPython 3.12** — `runtime/python/simkyt/simkyt.cpython-312-x86_64-linux-gnu.so`

If you are on a different platform or Python version, that `.so` will not load.
Prebuilt artifacts for macOS (Intel + Apple Silicon), Windows, and additional
Python versions are planned and will be published as release downloads. Until
then, if you need another target, open an issue — we can provide a build.

**Licensing.** placeKYT, the `gr_kyttar` library, and the GNU Radio blocks are open
source under **GPL-3.0**. The **simKYT** simulator binary is a closed Lattrex
component — its source is not published — but it is **free to use, and always will
be**. You do not need its source to use placeKYT. See [`LICENSE`](LICENSE) and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for details.

---

## 5. Upgrading

To update an existing install, run this from the repo root:

```bash
# 1. pull the latest code
git pull

# 2. refresh the placeKYT venv side (gr_kyttar + the prebuilt simkyt .so)
.venv/bin/pip install -e runtime/python

# 3. push the updated GNU Radio blocks INTO GNU Radio (see note below)
cd gr-kyttar && ./install.sh && cd ..
```

> **Step 3 is not optional if you use the GNU Radio integration.** The Kyttar
> GNU Radio blocks are *copied into* GNU Radio's own `site-packages` by
> `install.sh` — they do **not** run from the repo. A `git pull` updates the repo
> copy, but GNU Radio keeps loading the old copy until you re-run `./install.sh`.
> Skipping it means GNU Radio runs stale blocks (you'd see old behavior and think
> the update didn't take). If you only use the placeKYT GUI/CLI, you can stop
> after step 2.

Nothing needs to be compiled: the `simkyt` extension ships prebuilt, so an upgrade
is just `git pull` + the two install steps above. Because placeKYT (its venv + the
`.so`) and GNU Radio are fully decoupled — they only ever talk over the socket —
upgrading one never breaks the other.

---

## 6. Packaged installers — roadmap

Source install (above) is the supported path for this initial release. One-file,
double-click installers for **Windows, macOS, and Linux** are planned and will be
published as release downloads. Track progress in the repository's releases.
