"""
Kyttar Signal Source (NCO) Block for GNURadio

A numerically-controlled oscillator matching the on-chip NCOBlock: TRIGGER-
DRIVEN (one float input; each sample is a trigger whose value is ignored) with
ONE complex output — amplitude*exp(j*theta_n), theta_n =
2*pi*frequency/sample_rate*n. The trigger input is what keeps the oscillator
in sample lockstep with the stream that consumes it (the tremolo pattern);
n=0 emits phase 0, exactly GR's sig_source phase-0 start.

The real DSP runs on the placeKYT-hosted chip; this front-end generates the same
cos/sin host-side so a preview flowgraph shows the right thing.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class nco(gr.sync_block):
    """Kyttar Signal Source — trigger-driven complex oscillator (NCOBlock).

    ONE float input (the per-sample trigger; value ignored) -> ONE complex
    output ``amplitude*exp(j*theta_n)``. Mirrors the chip block exactly: the
    on-chip NCO produces one complex sample per input trigger.

    Parameters (mirroring GNU Radio's Signal Source):
        device_id:   which Kyttar device to use
        sample_rate: sample rate in Hz
        frequency:   tone frequency in Hz (the freq_word is derived internally)
        amplitude:   output amplitude (0..1)
        waveform:    "cos" (GR_COS_WAVE)
    """

    def __init__(self, device_id: str = "kyttar_0", sample_rate: float = 32000.0,
                 frequency: float = 2000.0, amplitude: float = 0.9,
                 offset: float = 0.0, phase: float = 0.0,
                 waveform: str = "cos"):
        gr.sync_block.__init__(
            self, name="Kyttar Signal Source",
            in_sig=[np.float32], out_sig=[np.complex64])
        self._device_id = device_id
        self._sample_rate = float(sample_rate)
        self._frequency = float(frequency)
        self._amplitude = float(amplitude)
        # GR sig_source_c offset (real DC bias on the cos/real channel) + initial
        # phase theta_0 in radians. Forwarded to the on-chip NCOBlock via _grc_advert.
        self._offset = float(offset)
        self._phase = float(phase)
        self._waveform = waveform
        self._n = 0     # sample counter for phase continuity across work() calls
        self._grc_advert = (str(device_id), "NCOBlock", {
            "sample_rate": sample_rate, "frequency": frequency,
            "amplitude": amplitude, "offset": offset, "phase": phase,
            "waveform": waveform})

    def set_frequency(self, frequency: float):
        self._frequency = float(frequency)

    def get_frequency(self) -> float:
        return self._frequency

    def start(self) -> bool:
        advert = getattr(self, "_grc_advert", None)
        if advert is not None:
            try:
                from ._batch_session import get_session
                device_id, placekyt_type, params = advert
                get_session(device_id).register_params(placekyt_type, params)
            except Exception:  # noqa: BLE001 — advertising is best-effort
                pass
        return True

    def work(self, input_items, output_items):
        # One complex sample per input TRIGGER (input values ignored).
        n = min(len(input_items[0]), len(output_items[0]))
        k = np.arange(self._n, self._n + n)
        theta = (2.0 * np.pi * self._frequency / self._sample_rate * k
                 + self._phase)
        out = (self._amplitude * np.exp(1j * theta)
               + self._offset).astype(np.complex64)
        output_items[0][:n] = out
        self._n += n
        return n
