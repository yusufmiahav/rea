"""Closed-loop simulation: mic -> desk EQ -> speaker -> room -> mic.

The room is a delay plus a resonance that pushes loop gain just over 1 at one
frequency, so the system howls unless something cuts that frequency. SimDesk is a
desk back-end that really applies the notches to the loop, so the whole chain
(detector, notch manager, desk) is tested end to end.
"""
from __future__ import annotations

import numpy as np

from .dsp import Biquad
from .notches import Notch, octaves_to_q


class SimDesk:
    name = "sim"

    def __init__(self, sr, n_bands=4):
        self.sr = sr
        self.bands = {}
        self.log = []

    def set_notch(self, n: Notch):
        old = self.bands.get(n.band)
        f = Biquad.peaking(self.sr, n.freq_hz, n.gain_db, octaves_to_q(n.width_oct))
        if old is not None:
            f.z1, f.z2 = old.z1, old.z2
        self.bands[n.band] = f
        self.log.append((n.band, n.freq_hz, n.gain_db))

    def clear_notch(self, band):
        self.bands.pop(band, None)

    def close(self):
        pass

    def process(self, x):
        for f in list(self.bands.values()):
            x = f.process(x)
        return x


def run_loop(source, sr, suppressor, desk: SimDesk, howl_hz=2000.0, loop_gain_db=1.0,
             delay=1024, block=512, broadband_gain=0.3):
    """Returns the mic signal. `suppressor` may be None (no feedback control)."""
    assert block <= delay
    room_res = Biquad.bandpass(sr, howl_hz, q=8.0, peak_gain=10 ** (loop_gain_db / 20) - broadband_gain)
    n = len(source)
    mic = np.zeros(n + delay)
    spk = np.zeros(n + delay)  # speaker output, delayed into the mic
    for s in range(0, n, block):
        e = min(n, s + block)
        back = spk[s:e]  # speaker output from `delay` samples ago
        fb = broadband_gain * back + room_res.process(back)
        m = source[s:e] + fb
        mic[s:e] = m
        if suppressor is not None:
            suppressor.process(m)
        out = desk.process(m.copy())
        spk[s + delay:e + delay] = np.clip(out, -4, 4)  # soft limit so the no-control case stays finite
    return mic[:n]
