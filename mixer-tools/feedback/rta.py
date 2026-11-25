"""Numbers for the analyser (RTA) display: the spectrum in 1/12-octave bands and the
response of the cuts the app has put on the desk."""
from __future__ import annotations

import math

import numpy as np

from .notches import geq_curve, octaves_to_q


def log_centres(lo=20.0, hi=20000.0, per_octave=12):
    n = int(round(math.log2(hi / lo) * per_octave)) + 1
    return lo * 2.0 ** (np.arange(n) / per_octave)


def band_levels(pdb: np.ndarray, bin_hz: float, centres: np.ndarray, per_octave=12) -> np.ndarray:
    """Highest FFT bin (dBFS) inside each fractional-octave band around each centre.
    Bands too narrow to hold a bin take the nearest bin."""
    half = 2.0 ** (0.5 / per_octave)
    out = np.empty(len(centres))
    n = len(pdb)
    for i, f in enumerate(centres):
        k0 = int(math.floor(f / half / bin_hz))
        k1 = int(math.ceil(f * half / bin_hz))
        k0, k1 = max(1, k0), min(n - 1, max(k1, k0 + 1))
        out[i] = pdb[k0:k1].max() if k1 > k0 else pdb[min(n - 1, int(round(f / bin_hz)))]
    return out


def _peaking_db(freqs, f0, gain_db, q, sr):
    A = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * f0 / sr
    al = math.sin(w0) / (2 * q)
    b = np.array([1 + al * A, -2 * math.cos(w0), 1 - al * A])
    a = np.array([1 + al / A, -2 * math.cos(w0), 1 - al / A])
    z = np.exp(-1j * 2 * np.pi * np.asarray(freqs) / sr)
    h = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return 20 * np.log10(np.abs(h) + 1e-12)


def _highpass_db(freqs, f0, sr):
    w0 = 2 * math.pi * f0 / sr
    al = math.sin(w0) / (2 * 0.7071)
    c = math.cos(w0)
    b = np.array([(1 + c) / 2, -(1 + c), (1 + c) / 2])
    a = np.array([1 + al, -2 * c, 1 - al])
    z = np.exp(-1j * 2 * np.pi * np.asarray(freqs) / sr)
    h = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return 20 * np.log10(np.abs(h) + 1e-12)


def eq_response_db(notches, freqs, geq_freqs=None, sr=96000.0) -> np.ndarray:
    """Total EQ change in dB at each frequency. With geq_freqs, the cuts are shown as the
    graphic EQ curve the desk gets (faders joined up); otherwise as parametric bells."""
    freqs = np.asarray(freqs, dtype=float)
    notches = list(notches)
    if geq_freqs is not None:
        faders = geq_curve(notches, geq_freqs)
        g = np.array([faders.get(i + 1, 0.0) for i in range(len(geq_freqs))])
        return np.interp(np.log2(freqs), np.log2(geq_freqs), g, left=0.0, right=0.0)
    out = np.zeros(len(freqs))
    for n in notches:
        if n.band == 0:  # HPF, 12 dB/octave
            out += _highpass_db(freqs, n.freq_hz, sr)
            continue
        out += _peaking_db(freqs, n.freq_hz, n.gain_db, octaves_to_q(n.width_oct), sr)
    return out
