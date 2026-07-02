"""Full-duplex modem END-TO-END via the GUI IMPORT + AUTO-P&R path (#334).

This proves the path the USER actually exercises in the GUI: import
``bpsk_modem.grc`` into a placeKYT project, **auto-place + auto-route** it
(no hand placement), build the chip, host it on a ``SimServer`` whose
``stream_targets`` are resolved straight from the placed/routed project, then
drive BOTH streams over a REAL socket via two ``process_batch`` RPCs:

  * stream ``'rx'``: the RRC BPSK I/Q burst (carrier + timing offset) → bits;
  * stream ``'tx'``: a TX bit burst → passband samples.

It DIFFERS from ``test_live_duplex_stream_id.py`` (which builds via
``engine.bpsk_modem_demo``'s EXPLICIT, congestion-free placement). Here the
placement comes entirely from ``AppController.import_grc`` →
``auto_place(use_bus="always")`` → ``auto_route_all(..., auto_orient=False)``
— i.e. the strategy-aware auto-P&R, the GUI deliverable. A BER-0 failure here
(while the explicit-placement duplex passes) is specifically an auto-P&R-path
issue and is REPORTED, never faked.

The build + host path mirrors the GUI's "Run as GNURadio Server"
(``SimController.start_gnuradio_server``): it calls ``ctrl.build()`` (the same
``BuildEngine`` the controller uses), loads ``result.words(0)`` into a fresh
``simkyt.Chip``, resolves ``stream_targets`` via ``engine.port_config``, and
hosts on a ``SimServer`` — all Qt-free.

The socket client + BER helper are reused from ``test_live_duplex_stream_id``
/ ``engine.bpsk_modem_demo`` so the recovered bits are directly comparable.

Run:
    QT_QPA_PLATFORM=offscreen \
      placekyt/.venv/bin/python -m pytest \
        placekyt/tests/test_modem_grc_import_duplex_e2e.py -x -q
"""

from __future__ import annotations

import os
import random
import socket

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from engine import bpsk_modem_demo as M  # noqa: E402
from engine.catalog import BlockCatalog  # noqa: E402
from engine.io.chip_type_io import load_chip_type  # noqa: E402
from engine.port_config import stream_targets as resolve_stream_targets  # noqa: E402
from engine.sim_bridge import SimServer, recv_message, send_message  # noqa: E402
from tests.conftest import CHIP_YAML as CT_PATH  # noqa: E402
from tests.conftest import EXAMPLES_DIR  # noqa: E402

GRC_MODEM = EXAMPLES_DIR / "bpsk_modem.grc"

pytestmark = pytest.mark.skipif(
    not (CT_PATH.exists() and GRC_MODEM.exists()),
    reason="chip yaml or modem .grc absent")


# ---- socket client + one-RPC helper (copied from test_live_duplex_stream_id) --
def _client(port):
    c = socket.socket()
    c.connect(("127.0.0.1", port))
    return c


def _batch(c, *, stream_id, payload, complex_, raw):
    """One process_batch RPC for a stream; returns (reply_header, out_array)."""
    send_message(c, {"op": "process_batch", "port": "x16_out",
                     "in_port": "x16_in", "stream_id": stream_id,
                     "complex": bool(complex_), "raw": bool(raw)},
                 np.asarray(payload, dtype=np.float32))
    return recv_message(c)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def imported(qapp):
    """Import bpsk_modem.grc, auto-place + auto-route (the GUI path), build the
    chip, and resolve stream_targets from the placed/routed project.

    Returns the AppController, BuildResult, and resolved stream_targets — all via
    the SAME Qt-free path the GUI "Run as GNURadio Server" uses (ctrl.build() →
    BuildEngine → words(0); stream_targets via engine.port_config)."""
    from ui.controller import AppController

    catalog = BlockCatalog.from_gr_kyttar()
    chip_type = load_chip_type(str(CT_PATH))

    ctrl = AppController(catalog=catalog)
    res = ctrl.import_grc(str(GRC_MODEM), chip_type="kyttar_10x12")
    assert res.ok, f"import failed, unknown blocks: {res.unknown}"
    assert len(ctrl.project.blocks) == 8

    # GUI default route strategy: full auto-P&R (the place<->route loop). The modem's
    # Costas `rotate` OUTPUT cell is BOXED in the compact placement (no free neighbour to
    # tap the bus), so net1 only routes after the loop re-folds Costas — via auto_pnr, not
    # a single auto_route_all pass.
    ctrl.auto_place(use_bus="always")
    rep = ctrl.auto_pnr({"kyttar_10x12": chip_type}, use_bus="always")
    # 11 routed nets: the importer SPLITS the complex MF→Costas link into its I and
    # Q nets (yi→xi, yq→xq) — a complex placeKYT block has two scalar input regs that
    # must BOTH be fed (GNURadio collapses the I/Q pair into one port), so the duplex
    # modem has one more net than the GRC's edge count.
    failed = [(r.name, r.reason) for r in rep.failed]
    assert rep.ok and len(rep.routed) == 11, \
        f"auto-route routed {len(rep.routed)}/11, failed: {failed}"

    # Build the chip the SAME way the GUI server does (ctrl.build() == app.build()).
    bres = ctrl.build()
    assert bres.ok, "build failed: " + "; ".join(str(e) for e in bres.errors)

    # Resolve stream_targets from the PLACED/ROUTED project (port_config), passing the
    # BUILD RESULT so each stream's injection cell/entry/hop comes from the ROUTED
    # corridor (ChipBuild.input_landings) — exactly as SimController.start_gnuradio_server
    # does at server start.
    targets = resolve_stream_targets(ctrl.project, ctrl.registry, ctrl.catalog, 0,
                                     build_result=bres)
    return ctrl, bres, targets


def test_stream_targets_resolved(imported):
    """The server's stream_targets (resolved from the auto-placed/routed project)
    must carry BOTH streams with the demo's tags: rx (out_tag 5, complex → 2 data
    regs) and tx (out_tag 10, real → 1 data reg)."""
    _ctrl, _bres, targets = imported
    print("\n[stream_targets]", targets)
    assert set(targets) == {"rx", "tx"}, f"streams={set(targets)}"
    assert targets["rx"]["out_tag"] == M.RX_TAG, targets["rx"]
    assert targets["tx"]["out_tag"] == M.TX_TAG, targets["tx"]
    # RX matched filter is complex → two input registers (xi/xq).
    assert len(targets["rx"]["data_addrs"]) == 2, targets["rx"]
    # TX mapper takes one bit operand.
    assert len(targets["tx"]["data_addrs"]) >= 1, targets["tx"]


@pytest.fixture(scope="module")
def driven(imported):
    """Drive BOTH streams over a REAL socket on the AUTO-PLACED+ROUTED chip and
    capture the replies. rx then tx, same hosted chip, NO reset between streams —
    mirrors test_live_duplex_stream_id (the server parks other-tag words in
    self._tag_buf so interleaved RPCs don't lose each other's words).

    Returns a dict of the resolved bits/passband + the demo's BER tuple, so the
    pass-facts and the recovery gates assert against ONE socket round-trip."""
    import simkyt

    _ctrl, bres, targets = imported

    # --- RX stimulus: RRC BPSK I/Q burst, carrier + timing offset (demo gens) --
    random.seed(5)
    rx_bits = [random.randint(0, 1) for _ in range(120)]
    sig, syms = M._tx_signal(rx_bits, timing_offset=0.45, amp=0.9)
    kk = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * 0.008 * kk)).astype(np.complex64)
    rx_payload = np.empty(2 * len(iq), dtype=np.float32)
    rx_payload[0::2] = iq.real
    rx_payload[1::2] = iq.imag

    # --- TX stimulus: a bit burst ---------------------------------------------
    tx_bits = [0, 1, 1, 0, 1, 0, 0, 1]
    tx_payload = np.asarray(tx_bits, dtype=np.float32)

    # Host the built chip the GUI-server way: fresh chip ← bres.words(0).
    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))

    srv = SimServer(chip, stream_targets=targets)
    p = srv.start()
    try:
        c = _client(p)
        # TWO process_batch RPCs over the real socket, one per stream (rx then tx,
        # same hosted chip, no reset — the existing duplex test's order).
        rx_h, rx_out = _batch(c, stream_id="rx", payload=rx_payload,
                              complex_=True, raw=True)
        tx_h, tx_out = _batch(c, stream_id="tx", payload=tx_payload,
                              complex_=False, raw=True)
        c.close()
    finally:
        srv.stop()

    rx = [int(round(v)) & 1 for v in (rx_out if rx_out is not None else [])]
    rx_ref = [0 if s > 0 else 1 for s in syms]
    e, m, lag = M._ber_with_lag(rx, rx_ref)
    tx_pb = [M._s16(int(v) & 0xFFFF) for v in (tx_out if tx_out is not None else [])]
    print(f"\n[RX] {len(rx)} bits recovered, BER={e}/{m} (lag={lag})")
    print(f"[TX] {len(tx_pb)} passband samples")
    return {"rx_h": rx_h, "tx_h": tx_h, "rx": rx, "ber": (e, m, lag),
            "tx_pb": tx_pb, "targets": targets}


def test_socket_roundtrip_both_streams_reply(driven):
    """The GUI-import → auto-P&R → build → host → drive path completes the FULL
    duplex round trip: both process_batch RPCs (over a real socket) succeed and
    the server echoes each stream's resolved out_tag. This is the path the user
    exercises in the GUI; it runs end-to-end (the recovery quality is gated
    separately below)."""
    assert driven["rx_h"]["ok"] and driven["rx_h"].get("out_tag") == M.RX_TAG, \
        driven["rx_h"]
    assert driven["tx_h"]["ok"] and driven["tx_h"].get("out_tag") == M.TX_TAG, \
        driven["tx_h"]


# ---------------------------------------------------------------------------
# RECOVERY GATES — the auto-P&R import path now recovers RX BER 0 for real.
#
# Three corridor-aware fixes (PART 1/PART 2 of #334) made this path work, each in
# the build/router/host layer (no DSP-block / simKYT change):
#   1. GRC-import complex-edge split: a complex MF→Costas link imports as ONE net
#      (only I), but a complex placeKYT block has two scalar input regs (xi/xq) that
#      must BOTH be fed — so the Q net (yq→xq) is synthesised and the Costas locks.
#   2. broker foreign-transit face: a broker cell a FOREIGN net merely transits must
#      restore to that net's forwarding face, else the foreign stream dies on the
#      broker's static fwd_face (the RX MF→Costas net diverted at the TX Upsampler→RRC
#      broker; the Slicer→x16_out egress diverted at the Costas→Gardner broker).
#   3. corridor-aware host injection: the build resolves each input net's injection
#      landing (cell/entry/hop/data_addrs) from the ROUTED corridor + broker entries
#      (ChipBuild.input_landings); stream_targets reads it so rx injects both operands
#      at the MF head and tx LANDS at its broker (the rx corridor pins the shared cell
#      EAST, so tx can't ride straight to the mapper any more).
# ---------------------------------------------------------------------------


def test_rx_recovers_ber0(driven):
    """RX recovers bits at BER 0 (lag-aligned, inversion-tolerant) over the FULL
    GUI-import → auto-P&R → build → host → socket path.

    Was xfail (the auto-P&R injection was inconsistent with the routed corridors).
    Now passes for real: the importer splits the complex MF→Costas link so the Costas
    gets BOTH I and Q (it locks); the build resolves every broker's foreign-transit
    face (so the RX corridor and chain are not mis-faced) and stashes each input net's
    routed-corridor injection landing; ``stream_targets`` reads that landing so the rx
    burst is injected at the MF head (both operands) and the tx burst lands at its
    broker — the corridor-aware injection the old xfail note called for."""
    e, m, lag = driven["ber"]
    assert m and e == 0, (
        f"RX stream BER={e}/{m} (lag={lag}); {len(driven['rx'])} bits — "
        f"targets={driven['targets']}")


def test_tx_returns_passband(driven):
    """TX returns a NON-EMPTY passband on the auto-P&R path (the task's TX gate).

    The tx burst now LANDS at its broker (the rx corridor pins the shared input cell
    EAST, so the tx word can't ride straight to the mapper) and the mapper→upsampler→
    RRC→IQUpconvert chain emits a passband burst (tag 10) on x16_out. This is a
    non-emptiness check; TX value-exactness vs the composed reference is the
    explicit-placement duplex's job (test_live_duplex_stream_id)."""
    assert driven["tx_pb"], \
        f"TX stream returned no passband samples — targets={driven['targets']}"


# ---------------------------------------------------------------------------
# FIX 2 (raw bit injection) + FIX 3 (per-tag output demux) gates.
#
# These re-host the built chip WITH TRACING enabled and drive both streams, so
# the trace can be ingested into a TraceModel and inspected. FIX 2 asserts the
# TX bit=1 injects RAW as 0x0001 (not Q15 0x7FFF) while RX still recovers BER 0;
# FIX 3 asserts BOTH x16_in and x16_out demux into one stream per tag.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def traced(imported):
    """Host the built chip WITH tracing on, drive rx then tx, and return the
    recovered bits/BER, the TX injected values, and the ingested TraceModel."""
    import simkyt

    from engine.trace_model import TraceModel

    _ctrl, bres, targets = imported

    random.seed(5)
    rx_bits = [random.randint(0, 1) for _ in range(120)]
    sig, syms = M._tx_signal(rx_bits, timing_offset=0.45, amp=0.9)
    kk = np.arange(len(sig))
    iq = (np.asarray(sig) * np.exp(1j * 2 * np.pi * 0.008 * kk)).astype(np.complex64)
    rx_payload = np.empty(2 * len(iq), dtype=np.float32)
    rx_payload[0::2] = iq.real
    rx_payload[1::2] = iq.imag

    tx_bits = [0, 1, 1, 0, 1, 0, 0, 1]
    tx_payload = np.asarray(tx_bits, dtype=np.float32)

    chip = simkyt.Chip.from_yaml(str(CT_PATH))
    chip.load_bitstream_physical(bres.words(0))
    chip.enable_trace(4_000_000)

    srv = SimServer(chip, stream_targets=targets)
    p = srv.start()
    try:
        c = _client(p)
        _rx_h, rx_out = _batch(c, stream_id="rx", payload=rx_payload,
                               complex_=True, raw=True)
        _tx_h, _tx_out = _batch(c, stream_id="tx", payload=tx_payload,
                                complex_=False, raw=True)
        c.close()
        events = chip.get_trace()
    finally:
        srv.stop()

    rx = [int(round(v)) & 1 for v in (rx_out if rx_out is not None else [])]
    rx_ref = [0 if s > 0 else 1 for s in syms]
    e, m, lag = M._ber_with_lag(rx, rx_ref)

    tm = TraceModel()
    tm.ingest(0, events, 10)
    # The last len(tx_bits) x16_in injections are the tx burst (rx ran first).
    inj = sorted((t.time_ns, t.data) for t in tm.transactions
                 if t.kind == "port_injection" and t.port == "x16_in")
    tx_inj = [d for _t, d in inj[-len(tx_bits):]]
    return {"ber": (e, m, lag), "tx_bits": tx_bits, "tx_inj": tx_inj, "tm": tm}


def test_fix2_tx_bit_injects_raw_and_ber0(traced):
    """FIX 2: the REAL burst path injects the TX bit RAW — bit 1 → 0x0001 (not the
    Q15 0x7FFF) — while RX still recovers BER 0 (the PSK mapper masks the LSB, so
    raw vs Q15 give the same bit)."""
    e, m, lag = traced["ber"]
    assert m and e == 0, f"RX BER={e}/{m} (lag={lag}) — FIX 2 must not regress it"
    # bit==1 injects as 0x0001, bit==0 as 0x0000 (raw, not Q15-scaled).
    for bit, val in zip(traced["tx_bits"], traced["tx_inj"]):
        assert val == (0x0001 if bit else 0x0000), \
            f"TX bit {bit} injected as 0x{val:04X} (expected raw)"
    assert any(b == 1 for b in traced["tx_bits"])  # the 0x0001 case is exercised


def test_fix3_both_ports_demux_by_tag(traced):
    """FIX 3: BOTH the x16 INPUT and OUTPUT ports demux into one stream per tag.
    x16_out (RX-bits tag 5 + TX-passband tag 10) previously collapsed to a single
    untagged trace because the port_capture event carries no dest — now the tag is
    recovered from the co-located data_arrival, so both output nets split."""
    tm = traced["tm"]
    in_tags = tm.port_tags(0, "x16_in")
    out_tags = tm.port_tags(0, "x16_out")
    # OUTPUT: the two modem output nets split by their WRITE dest tags 5 and 10.
    assert set(out_tags) == {M.RX_TAG, M.TX_TAG}, \
        f"x16_out tags={out_tags} (expected {{{M.RX_TAG}, {M.TX_TAG}}})"
    # INPUT: streams are keyed by (target_hop, dest) TOGETHER — both fields drive
    # how the hardware routes an injected word, so both determine stream identity.
    # The three input streams are the RX-I operand, the RX-Q operand, and the TX
    # bit stream. RX-Q and TX-bits both land at dest address 1, but their HOPS
    # differ (they route to different cells along the shared port), so keying by
    # dest alone WOULD have collapsed them onto one trace (analog samples overlaid
    # on digital bits). Keying by (hop, dest) keeps all three distinct.
    assert all(isinstance(t, tuple) and len(t) == 2 for t in in_tags), \
        f"x16_in tags should be (hop, dest) tuples, got {in_tags}"
    # Three distinct streams, and the addr-1 pair is separated by hop.
    assert len(set(in_tags)) == 3, f"x16_in tags={in_tags} (expected 3 streams)"
    addr1 = {t for t in in_tags if t[1] == 1}
    assert len(addr1) == 2, \
        f"the two dest-1 streams (RX-Q, TX-bits) must stay split by hop: {addr1}"
    # Every reported tag has samples (no empty phantom streams).
    by_tag = tm.port_streams_by_tag()
    for tag in out_tags:
        assert by_tag[(0, "x16_out", tag)], f"x16_out tag {tag} empty"
    for tag in in_tags:
        assert by_tag[(0, "x16_in", tag)], f"x16_in tag {tag} empty"


if __name__ == "__main__":
    app = QApplication.instance() or QApplication([])
    from ui.controller import AppController

    catalog = BlockCatalog.from_gr_kyttar()
    chip_type = load_chip_type(str(CT_PATH))
    ctrl = AppController(catalog=catalog)
    ctrl.import_grc(str(GRC_MODEM), chip_type="kyttar_10x12")
    ctrl.auto_place(use_bus="always")
    rep = ctrl.auto_route_all({"kyttar_10x12": chip_type},
                              use_bus="always", auto_orient=False)
    print(f"auto-route: ok={rep.ok} routed={len(rep.routed)}/11")
    bres = ctrl.build()
    targets = resolve_stream_targets(ctrl.project, ctrl.registry, ctrl.catalog, 0,
                                     build_result=bres)
    print("stream_targets:", targets)
