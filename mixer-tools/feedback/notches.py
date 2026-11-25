"""Turns feedback events into notch filters on a fixed set of desk EQ bands.

The desk has only a few parametric bands per channel, so the manager:
  * puts a new howl on a free band, as a narrow cut at a starting depth,
  * deepens an existing notch (and re-centres it) if the same frequency rings again,
  * stops at a maximum depth, and says when it runs out of bands.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field


def octaves_to_q(octaves: float) -> float:
    p = 2.0 ** octaves
    return math.sqrt(p) / (p - 1.0)


def q_to_octaves(q: float) -> float:
    return 2.0 / math.log(2.0) * math.asinh(1.0 / (2.0 * q))  # standard bandwidth relation


@dataclass
class Notch:
    band: int                 # index into NotchConfig.bands
    freq_hz: float
    gain_db: float
    width_oct: float
    hits: int = 1
    changed_at: float = 0.0


@dataclass
class NotchConfig:
    bands: list = field(default_factory=lambda: [1, 2])  # desk band ids to use
    start_depth_db: float = -6.0
    step_db: float = -3.0
    max_depth_db: float = -12.0
    width_oct: float = 1 / 9
    merge_oct: float = 1 / 6   # a new howl this close to a notch deepens that notch
    cooldown_s: float = 0.5    # ignore re-hits on a notch for this long after a change


class NotchManager:
    def __init__(self, config: NotchConfig | None = None, clock=time.monotonic):
        self.cfg = config or NotchConfig()
        self.clock = clock
        self.notches: dict[int, Notch] = {}

    def free_bands(self):
        return [b for b in self.cfg.bands if b not in self.notches]

    def handle(self, freq_hz: float):
        """Returns ("new"|"deeper", Notch), ("max", Notch), ("cooldown", Notch) or ("full", None)."""
        now = self.clock()
        c = self.cfg
        near = None
        for n in self.notches.values():
            if abs(math.log2(freq_hz / n.freq_hz)) <= c.merge_oct:
                if near is None or abs(math.log2(freq_hz / n.freq_hz)) < abs(math.log2(freq_hz / near.freq_hz)):
                    near = n
        if near is not None:
            if now - near.changed_at < c.cooldown_s:
                return "cooldown", near
            if near.gain_db <= c.max_depth_db:
                return "max", near
            near.gain_db = max(c.max_depth_db, near.gain_db + c.step_db)
            # Move toward the newly measured frequency, weighted by history.
            near.freq_hz = math.exp((math.log(near.freq_hz) * near.hits + math.log(freq_hz)) / (near.hits + 1))
            near.hits += 1
            near.changed_at = now
            return "deeper", near
        free = self.free_bands()
        if not free:
            return "full", None
        n = Notch(band=free[0], freq_hz=freq_hz, gain_db=c.start_depth_db, width_oct=c.width_oct, changed_at=now)
        self.notches[n.band] = n
        return "new", n

    def clear(self, band=None):
        if band is None:
            cleared = list(self.notches.values())
            self.notches.clear()
            return cleared
        n = self.notches.pop(band, None)
        return [n] if n else []

    def to_json(self) -> str:
        return json.dumps([asdict(n) for n in sorted(self.notches.values(), key=lambda n: n.band)], indent=2)

    def load_json(self, text: str):
        self.notches = {d["band"]: Notch(**d) for d in json.loads(text)}


class GeqManager(NotchManager):
    """Like NotchManager, but for a graphic EQ: each howl goes on the nearest fixed fader.
    cfg.bands is ignored; cfg.max_faders limits how many faders it may pull down."""

    def __init__(self, freqs, config: NotchConfig | None = None, max_faders=6, clock=time.monotonic):
        super().__init__(config, clock)
        self.freqs = list(freqs)
        self.max_faders = max_faders

    def free_bands(self):
        return []

    def handle(self, freq_hz: float):
        now = self.clock()
        c = self.cfg
        idx = min(range(len(self.freqs)), key=lambda i: abs(math.log2(freq_hz / self.freqs[i])))
        band = idx + 1
        n = self.notches.get(band)
        if n is not None:
            if now - n.changed_at < c.cooldown_s:
                return "cooldown", n
            if n.gain_db <= c.max_depth_db:
                return "max", n
            n.gain_db = max(c.max_depth_db, n.gain_db + c.step_db)
            n.hits += 1
            n.changed_at = now
            return "deeper", n
        if len(self.notches) >= self.max_faders:
            return "full", None
        n = Notch(band=band, freq_hz=self.freqs[idx], gain_db=c.start_depth_db, width_oct=1 / 3, changed_at=now)
        self.notches[band] = n
        return "new", n
