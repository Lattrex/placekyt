<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Kyttar Programming Guide

This guide describes the **programming model** of the Kyttar cell array: what a
cell is, the instruction set, the memory and configuration registers, the
fixed-point number format, and how DSP blocks are authored and placed with
placeKYT. It is what you need to write your own blocks and to read a simulation.

> This is the *programming* interface. The physical implementation of the cell
> (its circuits and timing) is intentionally out of scope — you never need it to
> program the array.

---

## 1. The model in one picture

A Kyttar chip is a 2-D grid of identical **cells**. Each cell:

- has **32 words** of 16-bit memory (`R0`–`R31`),
- runs a tiny program out of that same memory (code and data share the space),
- talks to its four neighbours — **South, East, West, North** — by sending words
  through a chosen output **face**.

There is **no global clock**. A cell does nothing until a neighbour pokes it;
then it runs its program to completion and goes quiet again. Data flows through
the fabric like water rippling over rocks in a stream: each cell does a small piece of the
computation and hands the result onward.

Two operations move work between cells:

- **WRITE** — send a data word to a neighbour, stored at a chosen register.
- **JUMP** — tell a neighbour to start executing at a chosen address.

A DSP block is just a small group of cells, each programmed to do one step, wired
together by WRITEs and JUMPs.

---

## 2. Q15 fixed-point

All arithmetic is **Q15**: a signed 16-bit value interpreting the top bit as sign
and the remaining 15 bits as a fraction in `[-1, +1)`. To convert:

```python
def float_to_q15(x):
    return int(round(max(-1.0, min(0.9999695, x)) * 32768)) & 0xFFFF

def q15_to_float(q):
    q = q - 0x10000 if q & 0x8000 else q   # sign-extend
    return q / 32768.0
```

`MULQ` / `MACQ` (the `Q` ALU modes) multiply two Q15 values and keep the correctly
scaled Q15 result. Always clip before converting to avoid overflow.

---

## 3. Registers and memory

Every cell has 32 words, `R0`–`R31`. Memory is **unified** — there is no
separation between program and data; instructions, coefficients, state, and delay
lines all live in the same 32 words.

| Register | Role |
|----------|------|
| `R0` | **Accumulator.** Every ALU operation writes its result here. |
| `R1`–`R31` | **General purpose.** Working registers, coefficients, state, delay lines, and program code — whatever your block needs. |

Conventions used by the placeKYT block library (not hardware rules, just
defaults you'll see):

- programs typically begin at `R1` (entry address 1), since `R0` is the accumulator;
- a single-word input is delivered to a high register (e.g. `R31`), with multiple
  inputs descending from there.

Execution **auto-halts** once the program counter runs past the last instruction,
so a straight-line program needs no explicit terminator. (`HALT` is also available
as an explicit opcode.)

### Configuration registers

A small set of **configuration registers** live in a **separate address space**
(they are *not* part of the 32-word memory; CONFIG address `N` is distinct from
`RN`). Access them with `MOVE` using bracket notation, e.g. `MOVE [FACE], R5` or
`MOVE R0, [FLAGS]`, or remotely with `WRITE.CFG`.

| Addr | Name | Access | Meaning |
|:----:|------|--------|---------|
| 0 | `FLAGS` | read | ALU status flags, 7 bits: `C, Z, N, V, P, A, SLT` (see §4.8) |
| 1 | `FACE` | read/write | Output direction for WRITE / JUMP: `0=South, 1=East, 2=West, 3=North` |
| 2 | `IN_FACE` | read | The face the most recent incoming JUMP arrived from |
| 3 | `LOCK_FACE` | read/write | When the arbiter lock is set, the only face inputs are accepted from |
| 4 | `LOCK` | read/write | Arbiter lock enable (1 = accept only `LOCK_FACE`, 0 = normal 4-way) |

`FACE` may be changed **mid-program** ("dynamic FACE switching") to send different
outputs in different directions from a single invocation — this is how a cell fans
out to multiple neighbours. The **arbiter lock** (`LOCK` / `LOCK_FACE`) forces a
cell to accept inputs from only one face at a time — used for multi-input
synchronization, where data from several faces must arrive in a defined order
regardless of timing.

---

## 4. The instruction set

Every instruction is **16 bits**. The top nibble `OP[15:12]` selects the
instruction; the remaining 12 bits are operand and mode fields. There are 16
opcodes (three are reserved and execute as `HALT`, so unprogrammed memory —
`0x0000` — is a safe `HALT`).

| OP | Mnemonic | Category | Operation |
|:--:|----------|----------|-----------|
| `0x0` | `HALT` | control | Stop execution. `0x0000` is `HALT` (reset-safe). |
| `0x4` | `MOVE` | data | `mem[DEST] = mem[SRC]` (or a CONFIG register). No flags. |
| `0x5` | `BR` | control | Conditional branch on a flag, optional invert, signed 6-bit offset. |
| `0x6` | `WRITE` | external | Send `R0` to a cell `@N` hops away, store at `DEST` (mem or CONFIG). |
| `0x7` | `JUMP` | external | Tell a cell `@N` hops away to begin executing at `DEST`. |
| `0x8` | `LOGIC` | ALU | `AND` / `OR` / `XOR` / `NOT` (MODE selects). Result → `R0`. |
| `0x9` | `ARITH` | ALU | `ADD` / `ADC` / `SUB` / `SBC` (MODE selects). Result → `R0`. |
| `0xA` | `SHL` | ALU | Shift / rotate **left** (barrel shifter, `ROT` bit). Result → `R0`. |
| `0xB` | `SHR` | ALU | Shift / rotate **right** (barrel shifter, `ROT` bit). Result → `R0`. |
| `0xC` | `MUL` | ALU | `MUL` / `MULQ` / `MULHI` (MODE selects). Result → `R0`. |
| `0xD` | `MAC` | ALU | `MAC` / `MACQ` / `MSU` / `MSUQ` (MODE selects). Accumulates in `R0`. |
| `0xE` | `CMP` | ALU | Flags from `SRC_A − SRC_B`. `R0` **unchanged**. |
| `0xF` | `LOAD` | data | Indirect load: `R0 = mem[mem[ADDR_REG] & 0x1F]` (table lookup, ≤32 entries). |

All ALU ops (`LOGIC`, `ARITH`, `SHL`/`SHR`, `MUL`, `MAC`, `CMP`) write their result
to `R0` (except `CMP`) and **update the flags**. The non-ALU ops (`HALT`, `MOVE`,
`BR`, `WRITE`, `JUMP`, `LOAD`) do **not** touch the flags.

### 4.1 LOGIC (`0x8`) — `MODE[11:10] | SRC_A[9:5] | SRC_B[4:0]`

| MODE | Mnemonic | Operation |
|:----:|----------|-----------|
| 00 | `AND` | `R0 = SRC_A & SRC_B` |
| 01 | `OR`  | `R0 = SRC_A \| SRC_B` |
| 10 | `XOR` | `R0 = SRC_A ^ SRC_B` |
| 11 | `NOT` | `R0 = ~SRC_A` (`SRC_B` ignored) |

Flags: `Z`, `N`, `P`, `A`, `SLT` set from the result; **carry and overflow cleared**.

```asm
AND Ra, Rb        ; R0 = Ra & Rb
OR  Ra, Rb        ; R0 = Ra | Rb
XOR Ra, Rb        ; R0 = Ra ^ Rb
NOT Ra            ; R0 = ~Ra
```

### 4.2 ARITH (`0x9`) — `MODE[11:10] | SRC_A[9:5] | SRC_B[4:0]`

| MODE | Mnemonic | Operation |
|:----:|----------|-----------|
| 00 | `ADD` | `R0 = SRC_A + SRC_B` |
| 01 | `ADC` | `R0 = SRC_A + SRC_B + C` |
| 10 | `SUB` | `R0 = SRC_A − SRC_B` |
| 11 | `SBC` | `R0 = SRC_A − SRC_B − ~C` |

Flags: **all** (`C`, `Z`, `N`, `V`, `P`, `A`, `SLT`). `C` = carry-out (add) /
borrow (sub); `V` = signed overflow.

```asm
ADD Ra, Rb        ; R0 = Ra + Rb
ADC Ra, Rb        ; R0 = Ra + Rb + carry      (multi-word add)
SUB Ra, Rb        ; R0 = Ra - Rb
SBC Ra, Rb        ; R0 = Ra - Rb - borrow     (multi-word subtract)
```

### 4.3 Shifts: SHL (`0xA`) / SHR (`0xB`) — `ROT[11] | CNT[9:6] | SRC[5:0]`

`ROT=0` shifts (fills with 0); `ROT=1` rotates (wraps). `CNT` is a 0–15 immediate,
or a register holding the count (register mode). Flags: all updated; `C` = the last
bit shifted/rotated out (0 when count is 0); `V` cleared.

```asm
SHL Rn            ; R0 = Rn << 1
SHL Rn, #imm      ; R0 = Rn << imm        (imm 0-15)
SHR Rn, #imm      ; R0 = Rn >> imm
ROL Rn, #imm      ; R0 = Rn rotated left  by imm
ROR Rn, #imm      ; R0 = Rn rotated right by imm
SHL Rn, [Rm]      ; R0 = Rn << (Rm & 0xF) (register count)
```

### 4.4 MUL (`0xC`) — `MODE[11:10] | SRC_A[9:5] | SRC_B[4:0]`

| MODE | Mnemonic | Operation | Use |
|:----:|----------|-----------|-----|
| 00 | `MUL`   | `R0 = (A × B) & 0xFFFF` | low 16 bits |
| 01 | `MULQ`  | `R0 = (A × B) >> 15` | **Q15** fixed-point product |
| 10 | `MULHI` | `R0 = (A × B) >> 16` | high 16 bits |

`MULQ` is the workhorse for Q15 DSP (see §2). Flags: all updated; `C` and `V` both
set when the signed 32-bit product doesn't fit in a signed 16-bit value.

### 4.5 MAC (`0xD`) — `MODE[11:10] | SRC_A[9:5] | SRC_B[4:0]`

Multiply-accumulate **into `R0`** — the FIR/IIR primitive.

| MODE | Mnemonic | Operation |
|:----:|----------|-----------|
| 00 | `MAC`  | `R0 = R0 + ((A × B) & 0xFFFF)` |
| 01 | `MACQ` | `R0 = R0 + ((A × B) >> 15)` (Q15) |
| 10 | `MSU`  | `R0 = R0 − ((A × B) & 0xFFFF)` |
| 11 | `MSUQ` | `R0 = R0 − ((A × B) >> 15)` (Q15) |

```asm
MACQ Ra, Rb       ; R0 += (Ra * Rb) >> 15     ; one FIR tap, Q15
MSUQ Ra, Rb       ; R0 -= (Ra * Rb) >> 15     ; e.g. complex mixer
```

### 4.6 CMP (`0xE`) and LOAD (`0xF`)

`CMP Ra, Rb` computes `Ra − Rb`, sets **all** flags, and leaves `R0` untouched —
use it to test before a `BR`. (When you need both the difference *and* the flags,
use `SUB`, which writes `R0`.)

`LOAD [Rn]` does a double-dereference: `R0 = mem[mem[Rn] & 0x1F]` — an indirect
table lookup (sine tables, coefficient banks, Baudot maps, up to 32 entries).

### 4.7 Branch (`0x5`) — `FLAG[11:9] | INV[8] | OFFSET[5:0]`

Branches relative: if the condition holds, `PC = PC + 1 + OFFSET` (signed 6-bit,
−32…+31). `INV=1` inverts the test (branch if the flag is **clear**).

| FLAG | Flag | `BR` (set) | `BR` inverted (clear) |
|:----:|------|-----------|------------------------|
| 000 | `C` Carry | `BR.C` | `BR.NC` |
| 001 | `Z` Zero | `BR.Z` / `BEQ` | `BR.NZ` / `BNE` |
| 010 | `N` Negative | `BR.N` / `BMI` | `BR.NN` / `BPL` |
| 011 | `V` Overflow | `BR.V` | `BR.NV` |
| 100 | `P` Parity | `BR.P` | `BR.NP` |
| 101 | `A` All-ones | `BR.A` | `BR.NA` |
| 110 | `SLT` Signed-less-than (`N ^ V`) | `BR.LT` | `BR.GE` |

### 4.8 The flags

The 7 status flags live in `CONFIG[0]` (`FLAGS`), read with `MOVE R0, [FLAGS]`:

| Bit | Flag | Meaning |
|:---:|------|---------|
| 0 | `C` | Carry-out (add) / borrow (sub) / last bit shifted out |
| 1 | `Z` | Result is zero |
| 2 | `N` | Result is negative (bit 15 set) |
| 3 | `V` | Signed overflow |
| 4 | `P` | Parity (XOR of all result bits) |
| 5 | `A` | All-ones (`0xFFFF`) |
| 6 | `SLT` | Signed less-than (`N ^ V`) |

### 4.9 External ops on the wire

- **`WRITE` sends two words** — the instruction, then the data from `R0` — so the
  receiving cell stores `R0` at `DEST`. **`JUMP` sends one** word and sets the
  target's PC. Output **direction** is set by the `FACE` config register, not by
  the instruction.
- A **local `WRITE`** (`@0`, i.e. `HOP_CNT = 31`) is exactly `MOVE mem[DEST], R0`.
- `WRITE.CFG` / a `CFG` bit targets the destination's **CONFIG** space instead of
  memory (e.g. set a neighbour's `FACE`).

### Relative addressing — the `@N` hop count

Cells are **not** addressed by absolute X,Y coordinates. Every WRITE / JUMP names
a *distance*: `@N` means "the cell `N` hops away in the current output `FACE`
direction." A 5-bit `HOP_CNT` field travels with the word; each cell it passes
through increments it, and the word is consumed at the cell where it reaches its
target. So a block is built from **relative** wiring — `WRITE @1, 0` means "store
`R0` into the next cell's `R0`," regardless of where the block ends up on the
array.

> Tip: to send a word that must *exit through an output port* `d` cells away, give
> it enough hops to transit all `d` cells and leave — i.e. `@(d+1)`.

---

## 5. Writing a block

A DSP block in the `gr_kyttar` library is a small Python class that declares, per
cell, an assembly **template** plus its inputs, outputs, state, and internal
wiring. The placement engine and the program **resolver** then handle register
allocation and turn the relative `@N` references into concrete WRITE/JUMP
addresses once the block is placed and routed — you write the algorithm, the tools
place it.

> **Want to write your own block?** See the dedicated
> **[BLOCK_AUTHORING_GUIDE.md](BLOCK_AUTHORING_GUIDE.md)** — it walks through the
> full block API (single-cell, multi-cell, feedback) and the GNU Radio Companion
> wrapper, end to end. The rest of this section is a conceptual sketch.

A single-cell gain block, conceptually:

```
; output = input * gain   (Q15)
start:
    MULQ  Rin, Rgain      ; R0 = input * gain
    WRITE @1, Rtarget     ; send R0 to the next block
    JUMP  @1, Rentry      ; trigger it
    HALT
```

Multi-cell blocks add **internal connections** (cell-to-cell WRITE/JUMP wiring)
and may close **feedback loops**. The key rule for feedback: don't close a loop
through a *trigger* (JUMP) path — have the relay cell read the fed-back value **as
data** (a WRITE) so the feedback completes independently of the forward path.

See the bundled blocks in `runtime/python/gr_kyttar/placement/` for worked
examples (gain, FIR, IIR biquad, NCO, complex mixer, RRC matched filter, a Costas
carrier-recovery loop, a Gardner timing-recovery loop, a BPSK slicer, and more).

---

## 6. From block to bitstream to simulation

The full flow, which placeKYT drives for you:

1. **Place** blocks on the cell array (by hand on the canvas, or auto-placed).
2. **Route** the connections between blocks (auto-router, or draw them).
3. **Build** the bitstream (`BitstreamGenerator`).
4. **Simulate** on simKYT — inject stimulus, run, read the output port.

Headlessly, that last part is one command:

```bash
python placekyt/cli.py --build my_design.kyt \
    --chip-type placekyt/resources/chips/kyttar_10x12.yaml -o my_design.kbs
python placekyt/cli.py --disasm my_design.kbs        # human-readable listing
```

In the GUI you get the same build plus a live view: per-cell execution, a
transaction log, a digital waveform viewer with cursors and a timeline scrubber,
and breakpoints.

---

## 7. Ports and the demo chip

The bundled demo chip `kyttar_10x12` is a 10×12 array with four ports:

| Port | Direction | Width | Location |
|------|-----------|:-----:|----------|
| `x16_in` | input | 16-bit | top-left |
| `x16_out` | output | 16-bit | right edge |
| `x1_in` | input | 1-bit | bottom-left |
| `x1_out` | output | 1-bit | bottom-right |

Ports use a simple request/acknowledge handshake: a port with a downstream
consumer is single-outstanding — the sender waits for the consumer to accept
before sending the next word. This backpressure is what paces data through a
design; you don't manage it manually.

---

## 8. Where to go next

- **[examples/coherent_bpsk_rx/](examples/coherent_bpsk_rx/)** — the runnable demo: a full coherent BPSK receiver, end to end.
- **[INSTALL.md](INSTALL.md)** — getting placeKYT running.
- **[BLOCK_AUTHORING_GUIDE.md](BLOCK_AUTHORING_GUIDE.md)** — write your own block.
- Read the block sources under `runtime/python/gr_kyttar/placement/` — every shipped DSP block is a concrete, tested example of this model.
