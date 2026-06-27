<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Writing a Kyttar DSP Block

> # ⛔ RULE #0 — MATCH THE GNU RADIO BLOCK EXACTLY. NO EXCEPTIONS.
>
> **A Kyttar block that claims a GNU Radio equivalent MUST expose the SAME
> PARAMETERS that GNU Radio block exposes — same knobs, same generality, same
> units.** "Matching" means *matching*. It does **not** mean "a useful subset",
> "the common cases", "three preset modes", or "an internal hardware proxy".
>
> Concretely, if the GNU Radio factory takes:
> - an **arbitrary table** (e.g. `chunks_to_symbols(symbol_table)`, a constellation
>   object, a `taps` list) → your block MUST accept an arbitrary table. NOT a fixed
>   enum of presets. NOT a hardcoded set.
> - a **real-world parameter** (e.g. `frequency` in Hz + `sample_rate`) → your block
>   MUST expose that real-world parameter. NEVER expose the hardware-internal proxy
>   (a `freq_word` phase increment, a raw Q15 coefficient) *instead*. Derive the
>   internal value from the GR-facing parameter inside the block.
> - a parameter with a **default** → use GNU Radio's exact default and name.
>
> ### The ONLY permitted deviation: a genuine HARDWARE constraint.
> You may narrow a parameter's range **only** when the chip's hardware truly cannot
> do what GR does (e.g. Q15 range `[-1, 1)`, 32 words per cell, finite cell count,
> one-output-per-input dataflow). When you do:
>
> 1. **It MUST be a real ISA/hardware limit — not "this was easier" or "the demo
>    only needs X".** If you can implement the full GR parameter within the ISA, you
>    MUST. ("It fits in 32 words" means there is NO excuse.)
> 2. **You MUST document it CLEARLY and LOUDLY** in THREE places:
>    - a `# HARDWARE DEVIATION:` comment at the param in the block's `__init__`,
>    - the block's class docstring, under a `Hardware deviations from <gr_block>:`
>      heading,
>    - the block's `notes` field in `verification/manifest.json`, prefixed
>      `HW-DEVIATION:`.
> 3. **You MUST raise a clear error** if the user passes a value the hardware can't
>    honor (never silently clamp or ignore it).
>
> ### What is FORBIDDEN (these are the exact mistakes that have happened):
> - ❌ Inventing a parameter abstraction GR doesn't have ("3 modes" instead of a
>   constellation table).
> - ❌ Splitting one GR block into two Kyttar blocks to dodge a parameter (e.g. a
>   separate "Decimator" instead of `decimation=` on the FIR — decimation is a GR
>   *parameter*, so it is a *parameter*).
> - ❌ Exposing a hardware-internal proxy in place of GR's real-world knob.
> - ❌ Marking a block `done` when only a subset of its parameter space is verified.
> - ❌ Deviating for ANY reason other than hardware **and** not saying so, loudly.
>
> ### Verification "done" bar:
> A block is `done` ONLY when its test sweeps the **whole declared parameter space**
> (every enum value; representative points across every continuous range; arbitrary
> tables exercised with several real tables), each compared against a GNU Radio
> golden built from the **same** parameters. One default config is NOT "done".
>
> If you are unsure whether a deviation is a real hardware limit: **STOP and ASK.**
> Do not decide unilaterally. Silent deviation is the single worst thing you can do
> here — it makes automated block generation impossible because the output cannot be
> trusted to mean what it says.

---

This guide walks through adding your **own** DSP block to placeKYT — from a
single-cell block to a multi-cell block with feedback, and (optionally) wrapping
it as a GNU Radio Companion block. It assumes you've read
**[PROGRAMMING_GUIDE.md](PROGRAMMING_GUIDE.md)** (the cell model, the instruction
set, Q15, and `@N` relative addressing).

> **Mental model.** A block is a small Python class that declares, *per cell*, an
> assembly **template** plus its named inputs, outputs, state, data (constants),
> and — for multi-cell blocks — how its cells are wired together. You write the
> algorithm symbolically; placeKYT's placer and **resolver** allocate the
> registers and turn your symbolic `{write:…}` / `{jump:…}` references into
> concrete WRITE/JUMP addresses once the block is placed and routed.

Each built-in block lives in its own module under
`runtime/python/gr_kyttar/placement/blocks/` (e.g. `gain.py`, `costas_loop.py`)
— open any of them alongside this guide; every shipped block is a worked example.
**You can add your own blocks without touching the placeKYT install at all** —
see [§7 External block libraries](#7-external-block-libraries).

---

## 1. The anatomy of a block

A block subclasses `KyttarBlock` and provides four things:

| Member | What it is |
|--------|-----------|
| `cell_count` (property) | How many cells the block occupies. |
| `interface` (property) | A `BlockInterface`: the entry address and input/output registers other blocks use to talk to it. |
| `build_cell_programs()` | Returns `{cell_index: CellProgram}` — the program for each cell. |
| `process_reference(samples)` | A floating-point reference implementation, used to validate the Q15 result in tests. |

Optional class attributes drive how it appears in the placeKYT block panel:

```python
CATEGORY = "signal_conditioning"          # the panel group
TAGS     = ["gain", "multiply"]           # search keywords
```

That's the whole contract. **Discovery is automatic**: the block library
imports every module under `placement/blocks/`, registers each concrete
`KyttarBlock` subclass it finds, and placeKYT's catalog
(`BlockCatalog.from_gr_kyttar()`) builds from that registry. To add a **built-in**
block you just drop a new `placement/blocks/<name>.py` defining your class — there
is no central list to edit. To add a block from **outside** the install, see §7.

---

## 2. A complete single-cell block

Here is the shipped `GainBlock` — the simplest real block, `output = input * gain`
in one cell. Read it top to bottom; the annotations explain every field.

```python
class GainBlock(KyttarBlock):
    CATEGORY = "signal_conditioning"
    TAGS = ["gain", "multiply", "signal_conditioning"]

    # How other blocks address this one:
    #   entry_address  - the JUMP target that starts this block (R1 by convention)
    #   input_registers - where incoming data lands (R31 by convention)
    #   output_registers - the register this block writes into the *next* block
    _interface = BlockInterface(entry_address=1,
                                input_registers=[31],
                                output_registers=[31])

    def __init__(self, name: str, gain: float = 0.5):
        super().__init__(name, gain=gain)
        # Convert the float parameter to a Q15 coefficient (see PROGRAMMING_GUIDE §2)
        self._gain_scaled = max(-32768, min(32767, int(round(gain * 32768)))) & 0xFFFF

    @property
    def cell_count(self) -> int:
        return 1

    @property
    def interface(self) -> BlockInterface:
        return self._interface

    def build_cell_programs(self) -> dict:
        return {0: CellProgram(
            inputs=[Port("sample", register=0)],   # the incoming sample arrives in R0
            outputs=[Port("out")],                  # one named output
            entries=[EntryPoint("default")],        # one entry point
            data=[DataWord("gain", self._gain_scaled, address=1)],  # the Q15 coefficient
            assembly_template="""\
start:
    MULQ R{in:sample}, R{data:gain}    ; R0 = sample * gain   (Q15)
    {write:out}                        ; send R0 to the next block
    {jump:out}                         ; trigger it
""",
        )}

    def process_reference(self, input_samples):
        return (input_samples * self._gain).astype(np.float32)
```

### The template substitution language

Inside `assembly_template`, you refer to your declared names with braces; the
resolver fills them in:

| Placeholder | Resolves to | Declared by |
|-------------|-------------|-------------|
| `R{in:NAME}` | the register an **input** landed in | `inputs=[Port("NAME", register=…)]` |
| `R{data:NAME}` | the register holding a **constant** | `data=[DataWord("NAME", value, address=…)]` |
| `R{state:NAME}` | a **scratch/state** register | `state=[StateVar("NAME")]` |
| `{write:NAME}` | a full `WRITE @hop, addr` to an **output** | `outputs=[Port("NAME")]` |
| `{jump:NAME}` | a full `JUMP @hop, addr` to an **output** | `outputs=[Port("NAME")]` |

The declarative fields (all from `gr_kyttar.placement.block`):

- **`Port(name, register=None)`** — a named input or output. `register=None`
  lets the resolver auto-allocate; pin it (e.g. `register=0`) when the hardware
  delivers the value to a specific register.
- **`DataWord(name, value, address=None, is_face=False)`** — a 16-bit constant
  (a Q15 coefficient, a table entry…). Set `is_face=True` if the value is a FACE
  direction code so it rotates correctly when the block is rotated.
- **`StateVar(name, register=None, initial_value=0)`** — a register the program
  reads and writes across its run (an accumulator, a delay element…).
- **`EntryPoint(name, address=None)`** — a place a JUMP can land. Most blocks
  have one (`"default"`); multi-entry blocks (routers, slicers) declare several.

You never write literal register numbers or hop counts for inter-cell traffic —
declare names and let the resolver place them. This is what makes a block
**relocatable**: it works wherever placeKYT puts it on the array.

---

## 3. Multi-cell blocks

When one cell isn't enough (a long FIR, a carrier loop, a Viterbi unit), return
**several** `CellProgram`s and declare how they're wired. Add these overrides:

```python
@property
def cell_count(self) -> int:
    return 3                      # this block is 3 cells

def build_cell_programs(self) -> dict:
    return {0: CellProgram(...),  # cell 0
            1: CellProgram(...),  # cell 1
            2: CellProgram(...)}  # cell 2

def internal_connections(self):
    # (src_cell, src_output_name, dst_cell, dst_input_name) — a DATA (WRITE) link
    return [(0, "out", 1, "in"),
            (1, "out", 2, "in")]

def internal_jumps(self):
    # (src_cell, jump_name, dst_cell, dst_entry_name) — a TRIGGER (JUMP) link
    return [(0, "go", 1, "default"),
            (1, "go", 2, "default")]

def default_layout(self):
    # Optional: a hand-tuned cell arrangement {cell_id: (dx, dy, face)}.
    # Omit it and the base class snakes the cells into a compact serpentine —
    # but that only makes the block COMPACT. It does NOT guarantee the layout
    # the router needs. For any multi-cell block you almost always must author
    # this to FOLD the block so input and output land on the SAME edge and a
    # wavefront's output exits the last cell. See the layout rules below.
    ...
```

> **Read the layout rules before laying out any multi-cell block:**
> **[`verification/KNOWLEDGE_BASE/layout_rules.md`](verification/KNOWLEDGE_BASE/layout_rules.md)**
> (and invariants INV-8/9/10). None of it is enforced by a DRC — a block that
> ignores it builds fine and then **silently fails to route**. In short: fold the
> block (don't lay it in a line); put the external input and output ports on the
> **same edge** so the routing bus can tap both; a wavefront block's output exits
> its **last** cell, not cell 0; and on this 10×12 chip keep the footprint **≤ 8
> cells across** in each direction so the bus has a channel to pass (a convention
> for this small chip, not an architectural rule).

`internal_connections` carries **data** (each is a WRITE); `internal_jumps`
carries **triggers** (each is a JUMP). The resolver turns both into the right
`@N` hops based on where the cells land.

### Feedback loops — the one rule that matters

Loops (PLLs, timing loops, IIR state) are fully supported, but:

> **Close a feedback loop through a DATA path, never a TRIGGER path.** Have the
> relay cell read the fed-back value **as data** (a WRITE into a state register)
> rather than as a JUMP. A loop closed through a trigger path couples the
> feedback to the forward execution and can deadlock; closed through data, the
> feedback completes independently.

The shipped `CostasLoopBlock` and the Gardner timing loop are concrete, tested
examples of this.

---

## 4. Test your block

A block isn't done until its Q15 output matches a reference. The pattern used
throughout the suite:

1. Build the block's programs, load them on simKYT, feed a known input, read the
   output port.
2. Compare against `process_reference()` (float) within a small tolerance
   (Q15 quantization is ±1–2 LSB), and — where a GNU Radio equivalent exists —
   against that too.

Put the test in `placekyt/tests/` and run it:

```bash
cd placekyt
QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m pytest tests/test_my_block.py -q
```

Once your class is registered (a `placement/blocks/<name>.py` for a built-in, or
discovered via §7 for an external one) it appears automatically in the placeKYT
block panel — place it, route it, build, and run.

---

## 5. (Optional) Expose it as a GNU Radio block

placeKYT discovers your block on its own. You only need a GNU Radio wrapper if you
want the block to appear in **GNU Radio Companion** (e.g. for the
flowgraph-driven stimulus/measurement workflow).

A GRC block is a `.block.yml` in `gr-kyttar/grc/`. Mirror the shipped
`kyttar_gain.block.yml`:

```yaml
id: kyttar_gain
label: Kyttar Gain
category: '[Kyttar]'
flags: [ python ]

documentation: |-
  Kyttar Gain — output = input * gain. Place between a Kyttar Source and Sink.

templates:
  imports: from gnuradio import kyttar
  make: kyttar.gain(device_id=${device_id}, gain=${gain})
  callbacks:
    - set_gain(${gain})

parameters:
  - id: device_id
    label: Device ID
    dtype: string
    default: '"kyttar_0"'
  - id: gain
    label: Gain
    dtype: real
    default: 0.5

inputs:
  - { label: in,  dtype: float }
outputs:
  - { label: out, dtype: float }

file_format: 1
```

Pair it with a small Python shim in `gr-kyttar/python/kyttar/` (see the existing
`gain.py`) that registers the block with the placeKYT device. The `id`, the
`make:` import, and the Python class name must agree.

---

## 6. Checklist

- [ ] Subclass `KyttarBlock` (in `placement/blocks/<name>.py` for a built-in, or your own module for an external block — §7).
- [ ] Implement `cell_count`, `interface`, `build_cell_programs()`, `process_reference()`.
- [ ] Declare inputs / outputs / state / data; reference them via `{in:}` `{data:}` `{state:}` `{write:}` `{jump:}` — no literal registers or hops for inter-cell traffic.
- [ ] Multi-cell? Add `internal_connections()` (+ `internal_jumps()` if you trigger between cells). Close feedback through **data**, not triggers.
- [ ] Set `CATEGORY` / `TAGS` for the panel.
- [ ] Add a test comparing simKYT output to `process_reference()` within ±1–2 LSB.
- [ ] *(optional)* Add a `.block.yml` + Python shim under `gr-kyttar/` for GNU Radio Companion.

---

## 7. External block libraries

You do not have to edit the placeKYT install to add blocks. The library
discovers external blocks two ways, so your blocks (or a whole third-party
library of them) load alongside the built-ins and appear in the catalog
automatically.

### a) A directory on `KYTTAR_BLOCK_PATH`

Point the `KYTTAR_BLOCK_PATH` environment variable at one or more directories
(colon-separated, like `PATH`). Every `*.py` in them is imported and any
`KyttarBlock` subclass it defines is registered:

```bash
export KYTTAR_BLOCK_PATH=~/my_kyttar_blocks:/opt/shared/kyttar_blocks
```

```python
# ~/my_kyttar_blocks/double.py
from gr_kyttar.placement.blocks import KyttarBlock, BlockInterface
from gr_kyttar.placement.block import CellProgram, Port, EntryPoint, DataWord

class DoubleBlock(KyttarBlock):
    CATEGORY = "user"
    TAGS = ["external"]
    _interface = BlockInterface(entry_address=1, input_registers=[31], output_registers=[31])
    def __init__(self, name): super().__init__(name)
    @property
    def cell_count(self): return 1
    @property
    def interface(self): return self._interface
    def build_cell_programs(self):
        return {0: CellProgram(
            inputs=[Port("x", register=0)], outputs=[Port("out")],
            entries=[EntryPoint("default")], data=[DataWord("two", 2, address=1)],
            assembly_template="start:\n    MUL R{in:x}, R{data:two}\n    {write:out}\n    {jump:out}\n")}
    def process_reference(self, v): return v * 2
```

Launch placeKYT (or run the CLI) with that variable set and `DoubleBlock` shows
up in the panel. No reinstall, no editing the package.

### b) A pip-installable package (entry points)

Ship a library of blocks as a normal Python package that advertises the
`gr_kyttar.blocks` entry-point group. Each entry point is imported and its
`KyttarBlock` subclasses are registered:

```toml
# pyproject.toml of your block package
[project.entry-points."gr_kyttar.blocks"]
my_library = "my_kyttar_library.blocks"   # a module (or any object) exposing the classes
```

After `pip install my-kyttar-library`, its blocks load automatically wherever
placeKYT runs. A plugin that fails to import is skipped (with a warning), never
fatal — one bad library can't break the catalog.

---

For deeper, real-world examples, read the FIR filter (multi-cell wavefront), the
Costas loop (feedback), and the BPSK slicer (multi-entry) under
`runtime/python/gr_kyttar/placement/blocks/`.
