"""Test signals: speech-like noise, harmonic music, and a building howl."""
from __future__ import annotations

import numpy as np


def speechlike(seconds, sr=48000, seed=1, level=0.1):
    """Syllable-rate bursts of noise, shaped by moving resonances (formant-ish)."""
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    noise = rng.standard_normal(n)
    out = np.zeros(n)
    seg = int(0.12 * sr)
    for start in range(0, n, seg):
        end = min(n, start + seg)
        x = noise[start:end]
        spec = np.fft.rfft(x)
        f = np.fft.rfftfreq(len(x), 1 / sr)
        shape = np.zeros_like(f)
        for fc in rng.uniform([300, 900, 2200], [900, 2200, 3500]):
            shape += np.exp(-0.5 * ((f - fc) / (fc * 0.15)) ** 2)
        shape += 0.05
        y = np.fft.irfft(spec * shape, len(x))
        env = np.sin(np.linspace(0, np.pi, len(x))) * rng.uniform(0.2, 1.0)
        out[start:end] = y * env
    return level * out / (np.abs(out).max() + 1e-12)


def music_chord(seconds, sr=48000, notes=(220.0, 277.2, 329.6), harmonics=6, vibrato_hz=5.5,
                vibrato_cents=20, level=0.3):
    t = np.arange(int(seconds * sr)) / sr
    out = np.zeros_like(t)
    for f0 in notes:
        dev = 2 ** (vibrato_cents / 1200 * np.sin(2 * np.pi * vibrato_hz * t)) - 1
        phase = 2 * np.pi * np.cumsum(f0 * (1 + dev)) / sr
        for h in range(1, harmonics + 1):
            out += np.sin(h * phase) / h
    return level * out / np.abs(out).max()


def howl(seconds, freq, sr=48000, start_s=1.0, start_db=-50.0, growth_db_s=12.0, max_db=-6.0):
    """A sine that starts at start_s and grows (like a loop just over unity gain)."""
    t = np.arange(int(seconds * sr)) / sr
    db = np.where(t < start_s, -200.0, np.minimum(max_db, start_db + growth_db_s * (t - start_s)))
    return 10 ** (db / 20) * np.sin(2 * np.pi * freq * t)
