# SPDX-License-Identifier: GPL-3.0-or-later
"""Low-level USB transport to the devkyt FPGA board (ZTEX USB-FPGA Module 2.18b).

This is the thin libusb/pyusb layer under :class:`~placekyt.engine.hw_chip.HwChip`.
It knows nothing about Kyttar semantics (WRITE/DATA/JUMP, hops, tags) — it only moves
raw 16-bit words to/from the FPGA over the FX3 bulk endpoints, and issues the FX3
control transfers for reset and endpoint discovery.

Grounded 1:1 in ZTEX's own libusb host reference (``ztex/capi/c/memfifo.c`` +
``ztex.c``) as documented in ``dev_docs/HARDWARE_BACKEND_PLAN.md`` §7. The FPGA side is
the devkyt gateware (``hs_tx``/``hs_rx`` + fake-gain/router); its ``.bit`` is PRE-FLASHED
to the ZTEX flash (auto-boots on power-up), so this module never uploads the gateware —
it only speaks the BULK data path + the app-reset control. (EP0 .bit upload, VR 0x31-0x35,
is deliberately NOT implemented; see the plan §7.)

Words are 16-bit, little-endian on the wire (matches the FX3 ``fd[15:0]`` slave-FIFO and
the ezusb_io ``DO``/``DI`` streams).
"""

from __future__ import annotations

import struct
from typing import List, Optional, Sequence

# ---- device identity (ZTEX default firmware) ----
ZTEX_VENDOR_ID = 0x221A
ZTEX_PRODUCT_ID = 0x0100

# ---- vendor control requests (from ztex.c) ----
_VR_RESET = 0x60          # app reset: wValue = (leave ? 1 : 0)
_VR_GPIO = 0x61           # GPIO ctl:  wValue = value, wIndex = mask, returns 8 bytes
_VR_IFACE_INFO = 0x64     # default-interface info: returns bulk EP addresses

# bmRequestType values
_RT_VENDOR_IN = 0xC0      # device->host, vendor, device
_RT_VENDOR_OUT = 0x40     # host->device, vendor, device

# ezusb_io streams 16-bit words; a partial (short) bulk read is normal.
_WORD_BYTES = 2


class HwTransportError(RuntimeError):
    """Any failure talking to the board (not found, USB error, protocol error)."""


class FX3Transport:
    """A connection to one ZTEX FX3 board.

    Lifecycle: ``connect()`` (find + configure + claim + discover endpoints), then
    ``send_words`` / ``recv_words`` / ``reset`` / ``ping``, then ``close()``. Use as a
    context manager to guarantee ``close()``.

    pyusb is imported lazily inside ``connect`` so that importing this module (and unit-
    testing the pure word-packing helpers) does not require a board or a libusb backend.
    """

    def __init__(
        self,
        vendor_id: int = ZTEX_VENDOR_ID,
        product_id: int = ZTEX_PRODUCT_ID,
        *,
        default_timeout_ms: int = 1000,
    ) -> None:
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.default_timeout_ms = default_timeout_ms
        self._dev = None            # usb.core.Device
        self._out_ep: Optional[int] = None   # host->FPGA bulk OUT address
        self._in_ep: Optional[int] = None    # FPGA->host bulk IN address

    # ------------------------------------------------------------------ connect
    def connect(self) -> None:
        """Find, open, configure, and claim the board; discover the bulk endpoints.

        Raises HwTransportError if the board is absent or cannot be claimed.
        """
        try:
            import usb.core
            import usb.util
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise HwTransportError(
                "pyusb is required for the hardware backend "
                "(`pip install pyusb`, and a system libusb-1.0)"
            ) from exc

        dev = usb.core.find(idVendor=self.vendor_id, idProduct=self.product_id)
        if dev is None:
            raise HwTransportError(
                f"ZTEX board not found (idVendor={self.vendor_id:#06x} "
                f"idProduct={self.product_id:#06x}). Is it plugged in and flashed?"
            )
        try:
            dev.set_configuration(1)
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as exc:
            raise HwTransportError(f"could not configure/claim the board: {exc}") from exc

        self._dev = dev
        self._discover_endpoints()

    def _discover_endpoints(self) -> None:
        """Read the OUT/IN bulk EP addresses via VR 0x64 (do NOT hardcode them).

        From ztex.c: buf[1] & 0x7F = default OUT ep; buf[2] | 0x80 = default IN ep.
        """
        buf = self._control_in(_VR_IFACE_INFO, length=128)
        if len(buf) < 3:
            raise HwTransportError(
                f"VR 0x64 interface-info read too short ({len(buf)} bytes); "
                "wrong firmware?"
            )
        self._out_ep = buf[1] & 0x7F
        self._in_ep = buf[2] | 0x80

    # -------------------------------------------------------------- word streaming
    def send_words(self, words: Sequence[int], *, timeout_ms: Optional[int] = None) -> int:
        """Send 16-bit words to the FPGA (host->FPGA bulk OUT). Returns words written."""
        self._require_connected()
        data = pack_words(words)
        n = self._dev.write(self._out_ep, data, self._timeout(timeout_ms))
        return n // _WORD_BYTES

    def recv_words(self, max_words: int, *, timeout_ms: Optional[int] = None) -> List[int]:
        """Read up to ``max_words`` 16-bit words (FPGA->host bulk IN).

        A short read is normal (returns whatever the FPGA had ready). On a USB timeout
        with no data, returns an empty list rather than raising — polling callers treat
        "nothing yet" as a normal outcome.
        """
        self._require_connected()
        try:
            raw = self._dev.read(self._in_ep, max_words * _WORD_BYTES, self._timeout(timeout_ms))
        except Exception as exc:  # usb.core.USBTimeoutError (pyusb) or USBError
            if _is_timeout(exc):
                return []
            raise HwTransportError(f"bulk IN read failed: {exc}") from exc
        return unpack_words(bytes(raw))

    # ----------------------------------------------------------------- control ops
    def reset(self, *, leave: bool = False) -> None:
        """Global app reset (VR 0x60). leave=False pulses; leave=True holds reset."""
        self._require_connected()
        self._control_out(_VR_RESET, w_value=1 if leave else 0)

    def gpio(self, value: int = 0, mask: int = 0) -> int:
        """Read/modify the 4 board GPIOs (VR 0x61). Returns the resulting GPIO byte."""
        self._require_connected()
        buf = self._control_in(_VR_GPIO, w_value=value, w_index=mask, length=8)
        return buf[0] if buf else 0

    def ping(self, probe_word: int = 0x1234, *, timeout_ms: int = 500) -> bool:
        """Active liveness check: round-trip a known word through the gateware.

        Recommended check (plan §7): send a word and read it back — proves the FPGA app
        is loaded + streaming, not just that the FX3 enumerated. The fake-gain/loopback
        gateware echoes/transforms it. Returns True on any returned word (the caller that
        needs an exact echo can compare); False on silence/error.
        """
        try:
            self.send_words([probe_word & 0xFFFF], timeout_ms=timeout_ms)
            got = self.recv_words(16, timeout_ms=timeout_ms)
        except HwTransportError:
            return False
        return len(got) > 0

    # --------------------------------------------------------------------- teardown
    def close(self) -> None:
        if self._dev is not None:
            try:
                import usb.util
                usb.util.release_interface(self._dev, 0)
                usb.util.dispose_resources(self._dev)
            except Exception:
                pass
            self._dev = None
            self._out_ep = self._in_ep = None

    def __enter__(self) -> "FX3Transport":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._dev is not None and self._out_ep is not None

    # ----------------------------------------------------------------- internals
    def _control_in(self, request: int, *, w_value: int = 0, w_index: int = 0, length: int = 0):
        return self._dev.ctrl_transfer(
            _RT_VENDOR_IN, request, w_value, w_index, length, self.default_timeout_ms
        )

    def _control_out(self, request: int, *, w_value: int = 0, w_index: int = 0) -> None:
        self._dev.ctrl_transfer(
            _RT_VENDOR_OUT, request, w_value, w_index, None, self.default_timeout_ms
        )

    def _timeout(self, timeout_ms: Optional[int]) -> int:
        return self.default_timeout_ms if timeout_ms is None else timeout_ms

    def _require_connected(self) -> None:
        if not self.connected:
            raise HwTransportError("transport not connected — call connect() first")


# ---- pure helpers (no device; unit-testable without pyusb/a board) ----

def pack_words(words: Sequence[int]) -> bytes:
    """16-bit words -> little-endian bytes (the FX3 fd[15:0] wire order)."""
    return struct.pack(f"<{len(words)}H", *[w & 0xFFFF for w in words])


def unpack_words(data: bytes) -> List[int]:
    """Little-endian bytes -> 16-bit words. Drops a trailing odd byte (short read)."""
    n = len(data) // _WORD_BYTES
    if n == 0:
        return []
    return list(struct.unpack(f"<{n}H", data[: n * _WORD_BYTES]))


def _is_timeout(exc: Exception) -> bool:
    """True if a pyusb exception is a benign read timeout (errno ETIMEDOUT=110)."""
    if exc.__class__.__name__ == "USBTimeoutError":
        return True
    return getattr(exc, "errno", None) == 110
