<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Installing the Kyttar GRC blocks

**The one command:** from `gr-kyttar/`, run `./install.sh`. It deploys the
canonical Python module (`python/kyttar/`) into the GNU Radio site-packages
`kyttar` dir AND all `grc/*.block.yml` into the GRC blocks dir(s), backs up
whatever it replaces, and clears the GRC parse cache.

```
./install.sh [--dry-run] [--no-sudo] [--py-dest DIR] [--grc-dest DIR]
```

- `--dry-run` — show what it would do, change nothing.
- `--py-dest` / `--grc-dest` — override the install targets.
- `--no-sudo` — refuse to escalate (fail instead if a target needs root).

The Python half typically lands in `~/.local/...` (no root); the GRC-blocks half
usually needs `sudo` for a system path like `/usr/local/share/gnuradio/grc/blocks/`.

Verify afterwards:

```bash
python3 -c 'from gnuradio import kyttar; print(kyttar.rx_batch)'
```

The single source of truth is `python/kyttar/` (the DSP blocks + source/sink +
`rx_batch` + the `placekyt_*` live-bridge modules) and `grc/*.block.yml`.

---

## Background: the GRC block-path precedence trap

The `.block.yml` files in this directory are the canonical, source-controlled GRC
block definitions — but GRC (gnuradio-companion) does **not** read this directory
by default. It scans several block paths, and a **later** path overrides an
earlier one (GRC logs `... loaded from A  overwritten by B`). A common precedence,
lowest → highest, is:

1. `~/.local/share/gnuradio/grc/blocks/` (user-local)
2. `/usr/local/share/gnuradio/grc/blocks/` (system; often highest precedence)

(`GRC_BLOCKS_PATH` does not reliably override these on all GRC versions.)

**The trap:** editing a `.block.yml` here has no effect in GRC if a stale copy
sits in a higher-precedence dir — that copy silently wins. A typical symptom is a
multi-port block that renders but draws **no wires**, with a log line like
`LookupError: sink key xi not in sink block keys` (the stale definition has
generic `in`/`out`/`0` ports, so the flowgraph's named connection keys don't
resolve).

GRC keys a port by its `id:` field (falling back to the label only if the `id`
is a digit). So every multi-port `.block.yml` must give each port an explicit
`id:` matching the keys used in the `.grc` connections — e.g. the Costas loop's
inputs `id: xi`, `id: xq` and output `id: yi_tap`.

`install.sh` deploys to **every** existing GRC block dir, so no stale copy in a
lower-precedence dir can shadow an edit. To do it by hand for a single block:

```bash
sudo cp grc/<block>.block.yml /usr/local/share/gnuradio/grc/blocks/
rm -rf ~/.cache/gnuradio/grc/*          # GRC caches parsed defs
```

Confirm GRC sees the right ports (no GUI needed):

```python
from gnuradio.grc.core.platform import Platform
from gnuradio import gr
p = Platform(version=gr.version(), version_parts=(3, 10, 0), prefs=gr.prefs())
p.build_library()
fg = p.make_flow_graph(); b = fg.new_block("kyttar_costas_loop")
print([x.key for x in b.sinks], [x.key for x in b.sources])   # -> ['xi','xq'] ['yi_tap']
```

---

## The `kyttar_rx_batch` block

`kyttar_rx_batch.block.yml` + `python/kyttar/rx_batch.py` implement the batch
bridge as a GRC-native block (**Kyttar RX (batch)**): it runs a whole complex
burst through a placeKYT-hosted chip in one `process_batch` RPC and emits the
decoded stream. It is a `gr.basic_block` (not a `sync_block`) because it
**decimates** — e.g. 319 I/Q samples → 159 decoded bits — and a sync block's 1:1
in/out-rate contract would corrupt the burst. The full demo flowgraph is
`examples/coherent_bpsk_rx_run.grc` (inline RRC-BPSK stimulus → Kyttar RX (batch)
→ QT GUI Time Sink).

---

## Packaging note

The repo `grc/` directory is the single source of truth; installing into the GRC
blocks path is a deployment step. A future packaging target should copy all of
`grc/*.block.yml` into the GRC blocks path so no stale subset can shadow an edit.
