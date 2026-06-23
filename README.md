<!--
SPDX-License-Identifier: GPL-3.0-or-later
Copyright (c) Lattrex. placeKYT and the Kyttar name and logo are trademarks of
Lattrex; see CONTRIBUTING.md for the brand-usage note.
-->

<p align="center"><img src="assets/banner.gif" alt="placeKYT" width="100%"></p>

# placeKYT

**The place-and-route + simulation IDE for the Kyttar asynchronous cell array.**

placeKYT is a visual design environment for the Kyttar processor — a 2-D grid of
identical, clockless compute cells that pass data to their neighbours like
nutrients through a network. You place DSP blocks on the array, route the
connections, build a bitstream, and run it on the bundled **simKYT** simulator —
watching data flow through the fabric cell-by-cell with a live transaction log,
waveform viewer, and cell inspector. A GNU Radio integration lets you drive a
placeKYT-hosted chip from a flowgraph for stimulus and measurement.

> Kyttar is a massively-parallel, asynchronous architecture aimed at real-time
> software-defined radio: place a chain of DSP blocks, and the array runs them in
> parallel with no global clock.

---

## What's in this repository

| Path | What it is |
|------|------------|
| `placekyt/` | The placeKYT IDE — Qt GUI, headless CLI, place/route/build engine, and the data model. |
| `gr-kyttar/` | A GNU Radio out-of-tree module: source/sink blocks that stream data to a placeKYT-hosted chip, plus runnable example flowgraphs. |
| `runtime/` | The simKYT runtime: the `gr_kyttar` block-build library (placement + bitstream generation) and the prebuilt `simkyt` simulator extension. |

---

## Quick look

- **Place & route** DSP blocks on the cell array — by hand on the canvas, or
  auto-placed and auto-routed.
- **Build** a Kyttar bitstream from your design.
- **Simulate** it on simKYT and watch it run: per-cell execution, a transaction
  log, a digital waveform viewer with cursors, a timeline scrubber, and
  breakpoints.
- **Import a GNU Radio flowgraph** of Kyttar DSP blocks and turn it into a placed
  design.
- **Drive it from GNU Radio**: host a chip in placeKYT and connect a flowgraph to
  it over a local socket for stimulus generation and waveform measurement —
  without hand-translating your design into a flowgraph.

---

## Getting started

placeKYT installs from source today (Linux + Python 3.12; other platforms and
one-file installers are on the roadmap — see **[INSTALL.md](INSTALL.md)**).

```bash
# 1. clone
git clone https://github.com/Lattrex/placekyt.git
cd placekyt

# 2. install (see INSTALL.md for the full, platform-specific steps)
python3 -m venv .venv
.venv/bin/pip install -r placekyt/requirements-dev.txt
.venv/bin/pip install -e runtime/python      # gr_kyttar + the prebuilt simkyt extension

# 3. launch the GUI
.venv/bin/python placekyt/main.py
```

Then open the demo in [`examples/coherent_bpsk_rx/`](examples/coherent_bpsk_rx/) —
a full coherent BPSK receiver. See [`INSTALL.md`](INSTALL.md) for the complete
GNU Radio + demo walkthrough.

To build a design headlessly and check it against a golden output:

```bash
.venv/bin/python placekyt/cli.py --test placekyt/tests/data/demo/qam16_demo.kyt \
    --chip-type placekyt/resources/chips/kyttar_10x12.yaml
# -> test PASSED: 12 output words match
```

---

## Documentation

- **[INSTALL.md](INSTALL.md)** — install from source (now) and the packaged-installer roadmap (Windows `.exe`/`.msi`, Linux `.AppImage`/`.deb`/`.rpm`, macOS `.app`).
- **[PROGRAMMING_GUIDE.md](PROGRAMMING_GUIDE.md)** — the Kyttar programming model: the instruction set, memory map, configuration registers, Q15 fixed-point, and how DSP blocks are written and placed. This is what you need to read a simulation.
- **[BLOCK_AUTHORING_GUIDE.md](BLOCK_AUTHORING_GUIDE.md)** — a step-by-step guide to writing your **own** DSP block (single-cell, multi-cell, feedback) and exposing it in GNU Radio Companion. Start here once you want to go beyond the bundled blocks.
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — how to contribute, run the tests, and a note on the simKYT simulator and Lattrex branding.
- **[gr-kyttar/examples/README.md](gr-kyttar/examples/README.md)** — the bundled demos.

---

## License

placeKYT and the `gr_kyttar` block library are released under the **GNU General
Public License v3.0 or later** (`GPL-3.0-or-later`) — see **[LICENSE](LICENSE)**.
This matches the GNU Radio ecosystem the GNU Radio integration plugs into.

The **simKYT** simulator is distributed as a prebuilt binary extension; it is a
Lattrex product and is **not** open-source. You may use it to run placeKYT and
the bundled blocks; you may not reverse-engineer or redistribute the binary on
its own. The Lattrex name, the Kyttar name, and associated logos are trademarks
of Lattrex (see CONTRIBUTING.md).
