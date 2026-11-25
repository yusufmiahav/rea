"""Acoustic feedback (howl) detector.

Works on blocks of mono audio and reports frequencies that look like feedback.
The tests are the classic ones from the feedback-suppression literature
(van Waterschoot & Moonen, "Fifty years of acoustic feedback control", 2011):

  PAPR  peak-to-average power ratio      a howl towers over the rest of the spectrum
  PNPR  peak-to-neighbouring power ratio  a howl is one sharp line, not a broad bump
  PHPR  peak-to-harmonic power ratio      a howl is close to a pure sine, so it has no
                                          strong harmonics (sung or played notes do)
  persistence + frequency stability       a howl stays put for hundreds of ms, while
                                          speech moves and vibrato wobbles
  IMSD-style growth check                 a howl holds steady or keeps growing

Only numpy is needed. Nothing here is taken from any commercial product.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class DetectorConfig:
    sample_rate: int = 48000
    fft_size: int = 8192          # ~5.9 Hz bins at 48 kHz
    hop_size: int = 1024          # ~21 ms per frame at 48 kHz
    min_freq: float = 60.0
    max_freq: float = 16000.0
    floor_db: float = -50.0       # ignore peaks quieter than this (dBFS); hum and room tone sit below
    papr_db: float = 16.0         # peak over mean spectrum power
    pnpr_db: float = 12.0         # peak over neighbours a few bins away
    phpr_db: float = 15.0         # peak over its 2nd/3rd harmonic
    max_peaks: int = 12           # strongest peaks examined per frame
    persist_s: float = 0.50       # how long a peak must hold before it counts (speech rarely holds a pitch this long)
    max_drift_hz: float = 3.0     # allowed wobble of the peak's frequency
    min_slope_db_s: float = -2.0  # feedback holds or grows; a peak fading faster than this is a voice or a note
    track_tolerance_bins: float = 1.5
    track_timeout_s: float = 0.08 # a track survives this long without its peak

    @property
    def frames_to_confirm(self) -> int:
        return max(2, int(round(self.persist_s * self.sample_rate / self.hop_size)))


@dataclass
class FeedbackEvent:
    freq_hz: float
    level_db: float
    papr_db: float
    pnpr_db: float
    phpr_db: float
    slope_db_s: float
    time_s: float


@dataclass
class _Track:
    bin: float
    freqs: list = field(default_factory=list)
    levels: list = field(default_factory=list)
    frames: list = field(default_factory=list)
    last_frame: int = 0
    reported: bool = False
    metrics: tuple = (0.0, 0.0, 0.0)


class FeedbackDetector:
    """Feed it audio with `process(block)`. It returns new FeedbackEvents."""

    def __init__(self, config: DetectorConfig | None = None):
        self.cfg = c = config or DetectorConfig()
        self.window = np.hanning(c.fft_size).astype(np.float64)
        # Scale so a full-scale sine reads about 0 dBFS at its peak bin.
        self.win_gain = self.window.sum() / 2.0
        self.bin_hz = c.sample_rate / c.fft_size
        self.kmin = max(4, int(c.min_freq / self.bin_hz))
        self.kmax = min(c.fft_size // 2 - 4, int(c.max_freq / self.bin_hz))
        self.buf = np.zeros(c.fft_size)
        self.pending = np.zeros(0)
        self.frame = 0
        self.tracks: list[_Track] = []
        self.last_pdb = None   # newest spectrum in dBFS (for the analyser display)
        self.last_seen = []    # peaks in the newest frame that look like feedback: [(freq_hz, dBFS)]

    # -- public -------------------------------------------------------------
    def reset(self):
        self.buf[:] = 0
        self.pending = np.zeros(0)
        self.tracks.clear()

    def forget(self, freq_hz: float):
        """Drop the track at this frequency so it can be re-reported after a notch."""
        k = freq_hz / self.bin_hz
        self.tracks = [t for t in self.tracks if abs(t.bin - k) > self.cfg.track_tolerance_bins * 2]

    def process(self, block: np.ndarray) -> list[FeedbackEvent]:
        block = np.asarray(block, dtype=np.float64).ravel()
        self.pending = np.concatenate([self.pending, block])
        hop = self.cfg.hop_size
        events = []
        while len(self.pending) >= hop:
            self.buf = np.roll(self.buf, -hop)
            self.buf[-hop:] = self.pending[:hop]
            self.pending = self.pending[hop:]
            self.frame += 1
            events.extend(self._analyse_frame())
        return events

    # -- internals ----------------------------------------------------------
    def _analyse_frame(self) -> list[FeedbackEvent]:
        c = self.cfg
        spec = np.fft.rfft(self.buf * self.window)
        mag = np.abs(spec) / self.win_gain
        pdb = 20 * np.log10(mag + 1e-12)
        band = pdb[self.kmin:self.kmax]
        mean_db = 10 * np.log10(np.mean(10 ** (band / 10)) + 1e-20)

        # Local maxima in range, strongest first.
        seg = pdb[self.kmin - 1:self.kmax + 1]
        is_peak = (seg[1:-1] > seg[:-2]) & (seg[1:-1] >= seg[2:])
        ks = np.nonzero(is_peak)[0] + self.kmin
        ks = ks[pdb[ks] > c.floor_db]
        ks = ks[np.argsort(pdb[ks])[::-1][:c.max_peaks]]

        seen = []
        for k in ks:
            level = pdb[k]
            papr = level - mean_db
            if papr < c.papr_db:
                continue
            nb = np.concatenate([pdb[k - 6:k - 2], pdb[k + 3:k + 7]])
            pnpr = level - nb.max()
            if pnpr < c.pnpr_db:
                continue
            phpr = self._phpr(pdb, k)
            if phpr < c.phpr_db:
                continue
            # Quadratic interpolation of the peak for sub-bin frequency.
            a, b, g = pdb[k - 1], pdb[k], pdb[k + 1]
            denom = a - 2 * b + g
            delta = 0.5 * (a - g) / denom if denom != 0 else 0.0
            seen.append((k + delta, level, (papr, pnpr, phpr)))

        self.last_pdb = pdb
        self.last_seen = [(kf * self.bin_hz, lv) for kf, lv, _ in seen]
        return self._update_tracks(seen)

    def _phpr(self, pdb: np.ndarray, k: int) -> float:
        """Peak over the strongest of its 2nd and 3rd harmonics (and the sub-harmonic,
        which catches a peak that is itself a harmonic of a played note)."""
        worst = -np.inf
        for mult in (2, 3, 0.5):
            h = int(round(k * mult))
            if 3 <= h < len(pdb) - 3:
                worst = max(worst, pdb[h - 2:h + 3].max())
        if worst == -np.inf:
            return np.inf
        return pdb[k] - worst

    def _update_tracks(self, seen) -> list[FeedbackEvent]:
        c = self.cfg
        events = []
        used = set()
        for kf, level, metrics in seen:
            best = None
            for i, t in enumerate(self.tracks):
                if i in used:
                    continue
                if abs(t.bin - kf) <= c.track_tolerance_bins and (best is None or abs(t.bin - kf) < abs(self.tracks[best].bin - kf)):
                    best = i
            if best is None:
                self.tracks.append(_Track(bin=kf))
                best = len(self.tracks) - 1
            used.add(best)
            t = self.tracks[best]
            t.bin = kf
            t.freqs.append(kf * self.bin_hz)
            t.levels.append(level)
            t.frames.append(self.frame)
            t.last_frame = self.frame
            t.metrics = metrics
            del t.freqs[:-64], t.levels[:-64], t.frames[:-64]

            ev = self._confirm(t)
            if ev:
                events.append(ev)

        timeout = max(1, int(c.track_timeout_s * c.sample_rate / c.hop_size))
        self.tracks = [t for t in self.tracks if self.frame - t.last_frame <= timeout]
        return events

    def _confirm(self, t: _Track):
        c = self.cfg
        n = c.frames_to_confirm
        if t.reported or len(t.frames) < n:
            return None
        frames = np.array(t.frames[-n:])
        # The last n hits must be (nearly) consecutive frames.
        if frames[-1] - frames[0] > n + 1:
            return None
        freqs = np.array(t.freqs[-n:])
        if freqs.max() - freqs.min() > 2 * c.max_drift_hz:
            return None
        levels = np.array(t.levels[-n:])
        secs = (frames - frames[0]) * c.hop_size / c.sample_rate
        slope = float(np.polyfit(secs, levels, 1)[0]) if secs[-1] > 0 else 0.0
        if slope < c.min_slope_db_s:
            return None
        t.reported = True
        papr, pnpr, phpr = t.metrics
        return FeedbackEvent(
            freq_hz=float(np.median(freqs)), level_db=float(levels[-1]),
            papr_db=float(papr), pnpr_db=float(pnpr), phpr_db=float(min(phpr, 99.0)),
            slope_db_s=slope, time_s=self.frame * c.hop_size / c.sample_rate,
        )
