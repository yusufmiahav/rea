"""Small DSP helpers (RBJ cookbook biquads), used by the closed-loop simulator."""
from __future__ import annotations

import math

import numpy as np


class Biquad:
    def __init__(self, b, a):
        self.b = [x / a[0] for x in b]
        self.a = [x / a[0] for x in a]
        self.z1 = self.z2 = 0.0

    @classmethod
    def peaking(cls, sr, f0, gain_db, q):
        A = 10 ** (gain_db / 40)
        w = 2 * math.pi * f0 / sr
        al = math.sin(w) / (2 * q)
        return cls([1 + al * A, -2 * math.cos(w), 1 - al * A], [1 + al / A, -2 * math.cos(w), 1 - al / A])

    @classmethod
    def highpass(cls, sr, f0, q=0.7071):
        w = 2 * math.pi * f0 / sr
        al = math.sin(w) / (2 * q)
        c = math.cos(w)
        return cls([(1 + c) / 2, -(1 + c), (1 + c) / 2], [1 + al, -2 * c, 1 - al])

    @classmethod
    def bandpass(cls, sr, f0, q, peak_gain=1.0):
        w = 2 * math.pi * f0 / sr
        al = math.sin(w) / (2 * q)
        return cls([al * peak_gain, 0.0, -al * peak_gain], [1 + al, -2 * math.cos(w), 1 - al])

    def process(self, x: np.ndarray) -> np.ndarray:
        b0, b1, b2 = self.b
        _, a1, a2 = self.a
        z1, z2 = self.z1, self.z2
        y = np.empty_like(x)
        for i, v in enumerate(x):  # transposed direct form II
            o = b0 * v + z1
            z1 = b1 * v - a1 * o + z2
            z2 = b2 * v - a2 * o
            y[i] = o
        self.z1, self.z2 = z1, z2
        return y
