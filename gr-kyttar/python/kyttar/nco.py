"""
Kyttar Signal Source (NCO) Block for GNURadio

A numerically-controlled oscillator that emits TWO real rails — cos and sin of
theta_n = 2*pi*frequency/sample_rate*n — matching the on-chip block (cos on yi,
sin on yq). Drop-in shape of GNU Radio's analog.sig_source_f cos/sin pair: a real
signal source with no stream input, so real blocks (e.g. an SSB Weaver's two
mixers) can tap each rail directly.

The real DSP runs on the placeKYT-hosted chip; this front-end generates the same
cos/sin host-side so a preview flowgraph shows the right thing.

Copyright 2026 Kyttar Computer Project.
SPDX-License-Identifier: GPL-3.0-or-later
"""

import numpy as np
from gnuradio import gr


class nco(gr.sync_block):
    """Kyttar Signal Source — real cos/sin oscillator (maps to NCOBlock).

    Two float outputs: ``cos`` = amplitude*cos(theta_n), ``sin`` =
    amplitude*sin(theta_n). No stream input (free-running source). On the Kyttar
    chip one cell emits both rails (yi=cos, yq=sin).

    Parameters (mirroring GNU Radio's Signal Source):
        device_id:   which Kyttar device to use
        sample_rate: sample rate in Hz
        frequency:   tone frequency in Hz (the freq_word is derived internally)
        amplitude:   output amplitude (0..1)
        waveform:    "cos" (GR_COS_WAVE)
    """

    def __init__(self, device_id: str = "kyttar_0", sample_rate: float = 32000.0,
                 frequency: float = 2000.0, amplitude: float = 0.9,
                 waveform: str = "cos"):
        gr.sync_block.__init__(
            self, name="Kyttar Signal Source",
            in_sig=[], out_sig=[np.float32, np.float32])
        self._device_id = device_id
        self._sample_rate = float(sample_rate)
        self._frequency = float(frequency)
        self._amplitude = float(amplitude)
        self._waveform = waveform
        self._n = 0     # sample counter for phase continuity across work() calls
        self._grc_advert = (str(device_id), "NCOBlock", {
            "sample_rate": sample_rate, "frequency": frequency,
            "amplitude": amplitude, "waveform": waveform})

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
        n = len(output_items[0])
        k = np.arange(self._n, self._n + n)
        theta = 2.0 * np.pi * self._frequency / self._sample_rate * k
        output_items[0][:] = (self._amplitude * np.cos(theta)).astype(np.float32)
        output_items[1][:] = (self._amplitude * np.sin(theta)).astype(np.float32)
        self._n += n
        return n
