<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# The SRAM panel + `SramControllerBlock` — the heterogeneous-memory contract

This is the contract that lets a Kyttar design use **external SRAM** it cannot hold in
32-word cells (INV-29): character tables, reverse maps, interleaver memory, long
correlator history. It models the next chip's vision — **embedded SRAM panels floating in
the array next to groups of cells** — with a *host-side* panel today (the FPGA implements
it in the demo; on-die SRAM in production).

The two pieces:

- **`SramPanelDevice`** (`placekyt/engine/sram_panel.py`) — the panel itself: a register
  file (R0–R7+) + an SRAM array. **Host-side**: it does NOT run in simkyt (a 16-bit array
  = 65 536 words can't live in cells). It bridges a chip **output** port (cells → panel:
  WRITE/JUMP traffic) to a chip **input** port (panel → cells: the push-read).
- **`SramControllerBlock`** (`runtime/.../blocks/sram_controller_block.py`) — a **1-cell
  hardened controller macro** that sits at the panel port and owns all panel sequencing, so
  upstream blocks just stream data. This is the KEEP block (the older `FpgaRamBlock` was
  deleted — see the 2026-08-07 lessons_log entry).

## 1. Why a panel, not cells

A cell is 32 words total (program + data + state); its `LOAD`-indirect table caps at ~21
entries (`mem[Rn] & 0x1F`). Anything table-heavy or long-memory exceeds that (INV-29). The
panel is *dumb storage + a tiny register protocol*; the controller cell drives it with the
EXISTING ISA — no new primitives, just `WRITE @N, dest` and `JUMP @N, entry` aimed at the
panel's port.

## 2. The panel register protocol (R0–R5+)

The controller addresses panel registers through the DEST field of a `WRITE` (data) or the
ENTRY field of a `JUMP` (trigger). **Triggers are JUMP-only; data is WRITE-only.**

| Reg | Kind | Meaning |
|----:|------|---------|
| **R0** | JUMP trigger | **Commit a write.** `JUMP @h,0` stores `R2` (payload) at the current address. |
| **R1** | JUMP trigger | **Issue a push-read.** `JUMP @h,1` reads `mem[addr]` and pushes it back per R3/R4. |
| **R2** | WRITE data | **Payload** — the word to store on the next R0 commit. |
| **R3** | WRITE data | **Read-out WRITE descriptor** — a raw 16-bit instruction word the panel re-emits verbatim on a read (see §3). |
| **R4** | WRITE data | **Read-out JUMP descriptor** — the raw JUMP word re-emitted after the data (see §3). |
| **R5, R6, …** | WRITE data | **Address** (low 16 bits in R5; R6+ extend for panels larger than 64 k words). |

A WRITE to R0/R1 is ignored (triggers are JUMP-only); a JUMP to R2+ is ignored.

## 3. The push-read (the decisive mechanism)

A read is **self-driven**: the controller does not poll. It pre-loads *where the answer
should go*, triggers, and the panel **originates** the delivery.

1. Controller `WRITE @h,3` = the **WRITE descriptor** `_wr(dest_hop, dest_reg)` — a raw
   `OP=WRITE | HOP_CNT=dest_hop | DEST=dest_reg` word. This says: deliver the read value to
   register `dest_reg` of the cell `dest_hop` hops into the chip **input** port the panel
   pushes to.
2. Controller `WRITE @h,4` = the **JUMP descriptor** `_jp(entry_hop, entry)` — the panel
   re-emits this JUMP after the data, so the destination cell can be *kicked* to run an
   entry once its register is loaded. (Disabled sentinel = HOP_CNT 31 / `@0` → data only.)
3. Controller `WRITE @h,5` = the read **address**.
4. Controller `JUMP @h,1` = the **read trigger**. The panel reads `mem[addr]` and injects a
   `WRITE(dest)=value` (+ optional `JUMP(entry)`) into the chip input port — i.e. the value
   comes back through a **DIFFERENT port** than the one the controller drives, routed by the
   descriptor's hop count to the destination cell. This is the "controller kicks off a
   WRITE+JUMP back out a different port" topology.

Descriptor word layout (matches the ISA descriptor-word encoding): `OP[15:12] | … | HOP_CNT[9:5] |
DEST[4:0]`. Helpers in the tests: `_wr(h,d) = (0x6<<12)|((h&0x1F)<<5)|(d&0x1F)`,
`_jp(h,e) = (0x7<<12)|((h&0x1F)<<5)|(e&0x1F)`. A descriptor with `HOP_CNT=31` (`@0`,
execute-locally) is the **disabled sentinel** — an R3 sentinel suppresses the whole read
delivery; an R4 sentinel delivers data with no follow-up JUMP.

## 4. `SramControllerBlock` — the 1-cell macro

Entries (`interface.entry_address=1`, input data in R25 auto-allocated):

- **`write`**: `WRITE wr_addr→R5`, `WRITE data→R2`, `JUMP→R0` (commit); `wr_addr++`.
- **`read`**: `WRITE rwd→R3`, `WRITE rjd→R4`, `WRITE rd_addr→R5`, `JUMP→R1` (trigger);
  `rd_addr++`.
- **`set_addr`**: load the incoming value into BOTH address counters (base/reset).

Params: `panel_hop` (@N hops from the cell to exit the panel port; default 1 = the cell
sits at the port), `read_wr_desc` / `read_jp_desc` (the raw R3/R4 push-read targets — where
reads land). **Auto-increment**: the upstream side sets a base once (or uses 0) and streams;
the controller bumps `wr_addr`/`rd_addr` each op. `RAW_OUTPUT_HOPS=True` so its literal
panel-protocol `WRITE/JUMP @N` hops are preserved (not @1-abutment-defaulted).

## 5. Runtime wiring (`PanelDriver`)

`PanelDriver(device, out_chip, out_port, in_chip, in_port)` binds a panel to real ports:
its **input** wires to a chip **output** port (cells→panel), its **output** to a chip
**input** port (panel→cells). Each `step()` drains the output port's WRITEs + JUMP triggers
**in time order** (so "WRITE address then JUMP trigger" resolves correctly), applies them,
and injects any push-read. **Single-outstanding, no FIFO**: the port is held-ack — the
controller stalls after each word until the panel accepts it (`release_output_ack`), so a
burst is never swallowed at once. `in_chip` may differ from `out_chip` (a panel can serve
several chips; the descriptors' hop counts route the delivery).

## 6. How a downstream block uses this (the recipe for the SRAM-backed blocks)

To hold a big table (e.g. the 1024-entry Varicode reverse map, or a Morse table):

1. **Load phase** (once): stream the table into the panel via the controller's `write`
   entry — `set_addr` to the base, then one `write` per entry; the controller
   auto-increments the address.
2. **Lookup phase** (per input): compute the index in a small cell, `set_addr`/point the
   controller's read address at the entry, set `read_wr_desc`/`read_jp_desc` so the panel
   pushes the looked-up word back to the consuming cell's register + kicks its entry, then
   `read`. The value arrives asynchronously via the push-read.

The table lives in the panel (unbounded), the *logic* stays in cells (small), and the
answer is delivered by the panel's self-driven WRITE+JUMP. That is the heterogeneous
compute-next-to-memory pattern the next chip generalizes to embedded panels in the array.

## 7. Verification

`placekyt/tests/test_sram_panel.py` (21 tests) is the gate: descriptor decode, write-commit,
latched/auto-increment address, multi-register address, the simkyt port API, a full-loop
integration, a **write-then-read-back-out-the-port round-trip through real routing**
(`0xCAFE` written to addr 3, read back, emerges on `x16_out`), the runnable `sram_demo.py`,
the single-outstanding/no-FIFO handshake, and the `PanelDriver` time-ordering. The demo
`engine/sram_demo.py` places the controller at the panel port and self-pumps a paced stream.
