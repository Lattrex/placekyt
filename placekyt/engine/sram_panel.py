"""Host-side SRAM / peripheral panel device (the SRAM panel notes).

A panel is a *host-side* device — it does NOT run inside simkyt. It bridges a
chip's **output port** (cells → panel: WRITE/JUMP traffic) to a chip's **input
port** (panel → cells: the push-read WRITE+DATA+JUMP traffic). The panel owns an
SRAM array and the R0–R7+ register file; cells address those registers through
the WRITE-dest / JUMP-entry fields of the instructions they emit toward the
panel (no new ISA primitives — see the SRAM panel notes §2).

Why host-side: a full 16-bit array (65 536 words) cannot live in 32-word cells,
so the panel can't be modelled as fabric cells. simkyt surfaces the traffic
it needs: ``read_port_with_channels`` gives ``(value, WRITE-dest)`` per data
word, and ``read_port_jumps`` (added for panels) gives the JUMP ``entry``
triggers. This class consumes both and drives the push-read via
``write_port_multi_i16``.

This module is Qt-free and engine-only so it can be unit-tested headless.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Register addresses (the SRAM panel notes §2). Triggers are JUMP-only; data is
# WRITE-only.
REG_WRITE_TRIGGER = 0   # R0: JUMP here → commit payload→addr
REG_READ_TRIGGER = 1    # R1: JUMP here → push-read via R3/R4
REG_PAYLOAD = 2         # R2: WRITE — data word to store
REG_READ_WR_DESC = 3    # R3: WRITE — raw WRITE descriptor word (emitted as-is)
REG_READ_JP_DESC = 4    # R4: WRITE — raw JUMP descriptor word (emitted as-is)
REG_ADDR_BASE = 5       # R5, R6, … : WRITE — address (low 16b first)

_OP_WRITE = 0x6
_OP_JUMP = 0x7
_LOCAL_HOP = 31         # HOP_CNT=31 == assembly "@0" == execute locally == no-op


@dataclass
class _DecodedDescriptor:
    """A decoded R3/R4 raw instruction word."""

    opcode: int
    hop_cnt: int
    dest: int           # DEST (WRITE) or entry (JUMP), bits[4:0]

    @property
    def is_noop(self) -> bool:
        """A descriptor with HOP_CNT=31 (@0) executes locally in the panel → does
        nothing (the disabled sentinel, the SRAM panel notes §3.1)."""
        return self.hop_cnt == _LOCAL_HOP


def _decode(word: int) -> _DecodedDescriptor:
    """Decode a 16-bit Kyttar instruction word into (opcode, hop_cnt, dest).

    Layout (encode.rs): ``OP[15:12] | … | HOP_CNT[9:5] | DEST[4:0]``.
    """
    word &= 0xFFFF
    return _DecodedDescriptor(
        opcode=(word >> 12) & 0xF,
        hop_cnt=(word >> 5) & 0x1F,
        dest=word & 0x1F,
    )


@dataclass
class SramPanelDevice:
    """Runtime state + behaviour of one SRAM panel.

    ``size_words`` sizes the array (default: a full 16-bit space). ``addr_regs``
    is how many 16-bit address registers (R5…) the size needs. The panel reads
    its trigger/data traffic from a chip output port and pushes read results into
    a chip input port; the wiring (which chip/port) is supplied per-step by the
    caller, since a panel may serve multiple chips.
    """

    size_words: int = 1 << 16
    addr_regs: int = 1
    # Latched register file. Addresses beyond the named ones are address-extension
    # words (R5, R6, …). Stored as a dict so sparse high addresses are cheap.
    regs: dict[int, int] = field(default_factory=dict)
    # The SRAM array, sparse (only written words are stored; reads default 0).
    mem: dict[int, int] = field(default_factory=dict)
    # Diagnostics — counts for tests / inspection.
    writes_committed: int = 0
    reads_issued: int = 0
    # Activity log for the inspector / blink visuals: the addresses touched
    # since the last drain, tagged "w" (write-commit) or "r" (read). Drained by
    # take_activity(); kept small (only the touched addresses, not every word).
    _activity: list = field(default_factory=list)

    def reg(self, addr: int) -> int:
        return self.regs.get(addr, 0) & 0xFFFF

    def take_activity(self) -> list:
        """Drain and return the (addr, kind) activity since the last call —
        ``kind`` ∈ {"w", "r"}. The inspector + panel blink consume this to flash
        the touched addresses; draining keeps the list bounded."""
        out, self._activity = self._activity, []
        return out

    # -- address assembly -----------------------------------------------------

    def _address(self) -> int:
        """Assemble the word address from R5 (low) + extension regs R6…,
        16 bits each, little-endian by register order."""
        addr = 0
        for i in range(self.addr_regs):
            addr |= self.reg(REG_ADDR_BASE + i) << (16 * i)
        return addr % max(1, self.size_words)

    # -- inbound traffic (cells → panel) --------------------------------------

    def on_write(self, dest: int, value: int) -> None:
        """A WRITE from a cell landed in panel register ``dest``. Data registers
        (R2+) latch the value; a WRITE to a trigger register (R0/R1) is ignored
        (triggers are JUMP-only, the SRAM panel notes §2)."""
        if dest in (REG_WRITE_TRIGGER, REG_READ_TRIGGER):
            return  # WRITE to a trigger is a no-op (logged by the caller if desired)
        self.regs[dest] = value & 0xFFFF

    def on_jump(self, entry: int):
        """A JUMP from a cell triggered panel register ``entry``. R0 commits a
        write; R1 issues a push-read. Returns the push-read emission for R1 (so
        the caller can inject it into the target chip), else ``None``. A JUMP to
        a data register (R2+) is ignored (data is WRITE-only)."""
        if entry == REG_WRITE_TRIGGER:
            self._commit_write()
            return None
        if entry == REG_READ_TRIGGER:
            return self._push_read()
        return None  # JUMP to a non-trigger register: ignored

    def _commit_write(self) -> None:
        addr = self._address()
        self.mem[addr] = self.reg(REG_PAYLOAD)
        self.writes_committed += 1
        self._activity.append((addr, "w"))

    def _push_read(self):
        """Build the push-read emission: read ``mem[addr]`` and describe the
        WRITE+DATA+JUMP the panel must inject into the fabric, per R3/R4.

        Returns ``None`` when the WRITE descriptor is the disabled sentinel
        (nothing to deliver), else a :class:`PushRead`."""
        wr = _decode(self.reg(REG_READ_WR_DESC))
        jp = _decode(self.reg(REG_READ_JP_DESC))
        addr = self._address()
        value = self.mem.get(addr, 0) & 0xFFFF
        self.reads_issued += 1
        self._activity.append((addr, "r"))
        if wr.is_noop:
            return None  # no data delivery requested
        return PushRead(
            value=value,
            dest=wr.dest,
            write_hop=wr.hop_cnt,
            jump_entry=(None if jp.is_noop else jp.dest),
            jump_hop=jp.hop_cnt,
        )

    def reset(self) -> None:
        self.regs.clear()
        self.mem.clear()
        self.writes_committed = 0
        self.reads_issued = 0
        self._activity.clear()


@dataclass
class PushRead:
    """A panel's read-out emission: deliver ``value`` to register ``dest`` of a
    cell ``write_hop`` hops into the target chip, then (unless disabled) JUMP to
    ``jump_entry`` ``jump_hop`` hops in. The caller injects this on the chip
    input port the panel's output wires to.
    """

    value: int
    dest: int
    write_hop: int
    jump_entry: int | None
    jump_hop: int


class PanelDriver:
    """Binds a :class:`SramPanelDevice` to real chip ports and pumps a step.

    A panel's **input** wires to a chip **output** port (cells → panel) and the
    panel's **output** wires to a chip **input** port (panel → cells). On each
    :meth:`step` the driver drains the chip output port's WRITEs + JUMP triggers
    in TIME ORDER (so "WRITE address then JUMP trigger" sequences resolve
    correctly), applies them to the device, and injects any push-read into the
    target chip input port.

    ``out_chip`` / ``out_port`` is the chip OUTPUT port the panel reads from.
    ``in_chip`` / ``in_port`` is the chip INPUT port the panel pushes into (may be
    a different chip — a panel can serve several; for multi-chip the descriptors'
    routing decides the cell). For the simple single-target case both are the
    same chip. ``set_hop`` is an optional ``(port, hop)`` callback to set the
    input port's target hop count from the read-out descriptor.
    """

    def __init__(self, device: SramPanelDevice, out_chip, out_port: str,
                 in_chip, in_port: str):
        self.device = device
        self.out_chip = out_chip
        self.out_port = out_port
        self.in_chip = in_chip
        self.in_port = in_port

    def step(self) -> int:
        """Process the panel's traffic with SINGLE-OUTSTANDING handshake (no
        FIFO, the SRAM panel notes / the no-FIFO rule).

        The chip output port feeding the panel is HELD-ACK: the controller stalls
        after EACH word until the panel accepts it. So each call consumes the
        words currently captured at the port (normally one), applies them to the
        device, and RELEASES the held ack per word so the controller may send the
        next — the panel never swallows a burst at once. A JUMP that triggers a
        read pushes the value back into the chip input port (also held-ack), and
        the panel does not accept further traffic until that push is accepted.

        Returns a "work done" count this step (words acked + push-reads
        injected) — the run loop keeps stepping while the panel is making
        progress, so the no-FIFO backpressure isn't mistaken for a finished run.
        """
        if not self._handshake_ports():
            return self._step_legacy()  # ports not marked held → old behaviour

        work = 0
        writes = self.out_chip.read_port_words_timed(self.out_port)
        jumps = self.out_chip.read_port_jumps(self.out_port)
        events = [(t, 0, ("w", dest, val)) for (val, dest, t) in writes]
        events += [(t, 1, ("j", entry)) for (entry, t) in jumps]
        events.sort(key=lambda e: (e[0], e[1]))
        for _t, _ord, ev in events:
            if ev[0] == "w":
                self.device.on_write(ev[1], ev[2])
            else:
                push = self.device.on_jump(ev[1])
                if push is not None:
                    self._inject(push)
            # Acknowledge this word back to the stalled controller cell so it
            # may emit the next — one word in flight at a time.
            self.out_chip.release_output_ack(self.out_port)
            work += 1
        # If the controller is stalled on a word we have NOT yet seen captured
        # (timing between run() and this pump), release it so the run advances
        # and surfaces it next step.
        if not events and self.out_chip.port_ack_pending(self.out_port):
            self.out_chip.release_output_ack(self.out_port)
            work += 1
        return work

    def _handshake_ports(self) -> bool:
        """True if the chip exposes the held-ack handshake API (post-#187)."""
        return hasattr(self.out_chip, "release_output_ack")

    def _step_legacy(self) -> int:
        """Pre-handshake drain-all behaviour (kept for chips without the held-ack
        API, e.g. older builds / the FakeChip in unit tests)."""
        writes = self.out_chip.read_port_words_timed(self.out_port)
        jumps = self.out_chip.read_port_jumps(self.out_port)
        events = [(t, 0, ("w", dest, val)) for (val, dest, t) in writes]
        events += [(t, 1, ("j", entry)) for (entry, t) in jumps]
        events.sort(key=lambda e: (e[0], e[1]))
        injected = 0
        for _t, _ord, ev in events:
            if ev[0] == "w":
                self.device.on_write(ev[1], ev[2])
            else:
                push = self.device.on_jump(ev[1])
                if push is not None:
                    self._inject(push)
                    injected += 1
        return injected

    def _inject(self, push: PushRead) -> None:
        """Inject a push-read into the target chip input port: a WRITE(dest)=value
        burst followed by a JUMP(entry). Uses ``write_port_multi_i16`` so one
        call delivers WRITE+DATA+JUMP atomically (entry=None → no JUMP)."""
        # The input port's hop count routes the burst to the addressed cell.
        try:
            self.in_chip.set_port_target_hop_count(self.in_port, push.write_hop)
        except Exception:  # noqa: BLE001 — older API / fixed-hop port
            pass
        entry = push.jump_entry if push.jump_entry is not None else 0
        self.in_chip.write_port_multi_i16(
            self.in_port, [[(push.dest, push.value & 0xFFFF)]], entry)
