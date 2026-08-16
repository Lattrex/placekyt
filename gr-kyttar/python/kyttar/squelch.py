"""
Kyttar Squelch Block for GNURadio

A signal level gate (squelch) block; gates the signal based on estimated power.

GR marker; the real DSP runs on the placeKYT-hosted chip. This block keeps the
exact GR interface (class name, params, ports) so it places/wires identically in
GRC, but does NO in-process placement and streams pure pass-through.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

from .dsp_markers import _PassThrough


class squelch(_PassThrough):
    """
    Kyttar Squelch - Signal Level Gate

    Gates the signal based on power level on the chip.
    GR marker; the real DSP runs on the placeKYT-hosted chip.

    Parameters (mirror GNU Radio analog.pwr_squelch_ff VERBATIM):
        device_id: ID of the kyttar.device to use
        db: threshold in dB for power squelch (GR default -50)
        alpha: gain of the power averaging filter (GR default 1e-4)
        ramp: attack/release ramp in samples (0 = disabled)
        gate: True = drop squelched samples (unsupported); False = emit zeros
    """

    def __init__(
        self,
        device_id: str = "kyttar_0",
        db: float = -50.0,
        alpha: float = 0.0001,
        ramp: int = 0,
        gate: bool = False,
    ):
        super().__init__(name="Kyttar Squelch", n_in=1, n_out=1)
        self._device_id = device_id
        self._db = db
        self._alpha = alpha
        self._ramp = int(ramp)
        self._gate = bool(gate)
        # Advertise params for GRC↔placeKYT sync detection (see dsp_markers).
        # Names are the SquelchBlock/analog.pwr_squelch_ff class params VERBATIM.
        self._advertise_grc_params(
            device_id, "SquelchBlock",
            {"db": db, "alpha": alpha, "ramp": int(ramp), "gate": bool(gate)})

    def set_db(self, db: float):
        """Set squelch threshold in dB."""
        self._db = db

    def set_alpha(self, alpha: float):
        """Set power-averaging filter gain."""
        self._alpha = alpha

    def get_db(self) -> float:
        """Get current threshold in dB."""
        return self._db

    def get_alpha(self) -> float:
        """Get current averaging alpha."""
        return self._alpha
