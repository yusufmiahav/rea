"""Turns feedback events into notch filters on a fixed set of desk EQ bands.

The desk has only a few parametric bands per channel, so the manager:
  * puts a new howl on a free band as a bell cut (1/6 octave, -6 dB to start),
  * keeps that band on its frequency: if it rings again the bell gets deeper and wider,
  * if a howl rings close by, stretches the same bell to cover both instead of moving
    the band or using another one,
  * stops at a maximum depth and width, and says when it runs out of bands.
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


HPF_BAND = 0  # a Notch on band 0 means "the channel's high-pass filter at freq_hz"


@dataclass
class Notch:
    band: int                 # desk band number (PEQ 1-4, or GEQ fader 1-28)
    freq_hz: float
    gain_db: float
    width_oct: float
    hits: int = 1
    changed_at: float = 0.0
    lo_hz: float = 0.0        # lowest and highest howl this cut covers
    hi_hz: float = 0.0

    def __post_init__(self):
        self.lo_hz = self.lo_hz or self.freq_hz
        self.hi_hz = self.hi_hz or self.freq_hz

    def distance_oct(self, f: float) -> float:
        """How far f is outside the span this cut covers (0 if inside)."""
        if self.lo_hz <= f <= self.hi_hz:
            return 0.0
        return min(abs(math.log2(f / self.lo_hz)), abs(math.log2(f / self.hi_hz)))


@dataclass
class NotchConfig:
    bands: list = field(default_factory=lambda: [2, 3])  # desk band numbers to use
    start_depth_db: float = -6.0
    step_db: float = -3.0
    max_depth_db: float = -12.0
    width_oct: float = 1 / 6       # starting width: a visible bell, not a pin-prick
    widen_step: float = 1.25       # each repeat ring widens the bell by this factor
    max_width_oct: float = 1.0
    merge_oct: float = 1 / 3       # a howl this close to a cut reshapes that cut, never a new band
    spill_oct: float = 2 / 3       # when every band is used, a howl this close widens the nearest cut
    lock_oct: float = 1 / 24       # within this of the centre the band stays put and just deepens
    cooldown_s: float = 0.5        # ignore re-hits on a cut for this long after a change
    hpf_max_hz: float = 0.0        # >0: a howl low enough is handled by raising the HPF, up to this
    hpf_margin_oct: float = 0.5    # the HPF goes this far above the howl (12 dB/oct -> about 6-8 dB down)
    hpf_step_oct: float = 1 / 3    # each repeat ring below the HPF raises it this much more


class NotchManager:
    """Each band, once given a frequency, stays there. Repeat rings make that cut deeper
    and wider (a smooth bell); nearby rings stretch the bell to cover them instead of
    moving the band or taking another one."""

    def __init__(self, config: NotchConfig | None = None, clock=time.monotonic):
        self.cfg = config or NotchConfig()
        self.clock = clock
        self.notches: dict[int, Notch] = {}

    def free_bands(self):
        return [b for b in self.cfg.bands if b not in self.notches]

    def _hpf(self, freq_hz, now):
        """Raise the HPF for a low howl. Returns (action, Notch) or None if the HPF can't help."""
        c = self.cfg
        if c.hpf_max_hz <= 0 or freq_hz * 2 ** c.hpf_margin_oct > c.hpf_max_hz * 2 ** (1 / 6):
            return None
        cur = self.notches.get(HPF_BAND)
        want = freq_hz * 2 ** c.hpf_margin_oct
        if cur is not None:
            if now - cur.changed_at < c.cooldown_s:
                return "cooldown", cur
            # Still ringing with the HPF in: go up at least one more step.
            want = max(want, cur.freq_hz * 2 ** c.hpf_step_oct)
            if cur.freq_hz >= c.hpf_max_hz:
                return None          # at the limit: let a PEQ band take it
        want = min(c.hpf_max_hz, want)
        if cur is None:
            cur = Notch(band=HPF_BAND, freq_hz=want, gain_db=0.0, width_oct=0.0, changed_at=now)
            self.notches[HPF_BAND] = cur
        else:
            cur.freq_hz, cur.hits, cur.changed_at = want, cur.hits + 1, now
        cur.lo_hz = min(cur.lo_hz, freq_hz)
        return "hpf", cur

    def _nearest(self, freq_hz, limit):
        best = None
        for n in self.notches.values():
            if n.band == HPF_BAND:
                continue
            d = n.distance_oct(freq_hz)
            if d <= limit and (best is None or d < best[0]):
                best = (d, n)
        return best[1] if best else None

    def _reshape(self, n: Notch, freq_hz: float, now: float, extra: float = 0.0):
        c = self.cfg
        if now - n.changed_at < c.cooldown_s:
            return "cooldown", n
        n.lo_hz, n.hi_hz = min(n.lo_hz, freq_hz), max(n.hi_hz, freq_hz)
        span = math.log2(n.hi_hz / n.lo_hz)
        if abs(math.log2(freq_hz / n.freq_hz)) <= c.lock_oct or span <= c.lock_oct:
            if n.gain_db <= c.max_depth_db and n.width_oct >= c.max_width_oct:
                return "max", n
            n.gain_db = max(c.max_depth_db, n.gain_db + c.step_db - extra / 2)
            n.width_oct = min(c.max_width_oct, n.width_oct * c.widen_step)
            action = "deeper"
        else:
            # Centre the bell between the lowest and highest howl and make it wide enough for both.
            n.freq_hz = math.sqrt(n.lo_hz * n.hi_hz)
            n.width_oct = min(c.max_width_oct, max(n.width_oct, span + c.width_oct))
            n.gain_db = max(c.max_depth_db, n.gain_db + (c.step_db - extra / 2) / 2)
            action = "wider"
        n.hits += 1
        n.changed_at = now
        return action, n

    def _start_depth(self, severity_db):
        return max(self.cfg.max_depth_db, self.cfg.start_depth_db - max(0.0, severity_db))

    def handle(self, freq_hz: float, severity_db: float = 0.0):
        """severity_db (0 or more) makes a worse howl get a deeper cut: a new cut starts that much
        deeper and a repeat ring deepens by half of it extra, never past max_depth_db.
        Returns (action, Notch). action is "new", "deeper", "wider", "max", "cooldown" or "full"
        (Notch is None for "full")."""
        now = self.clock()
        c = self.cfg
        hp = self._hpf(freq_hz, now)
        if hp is not None:
            return hp
        near = self._nearest(freq_hz, c.merge_oct)
        if near is not None:
            return self._reshape(near, freq_hz, now, severity_db)
        free = self.free_bands()
        if free:
            n = Notch(band=free[0], freq_hz=freq_hz, gain_db=self._start_depth(severity_db), width_oct=c.width_oct, changed_at=now)
            self.notches[n.band] = n
            return "new", n
        near = self._nearest(freq_hz, c.spill_oct)
        if near is not None:
            return self._reshape(near, freq_hz, now, severity_db)
        return "full", None

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
    """For a graphic EQ. Each howl becomes a cut keyed by its nearest fader, but the cut keeps
    the real howl frequency and a width, so the desk can spread it over the neighbouring
    faders as a smooth curve (see geq_curve). cfg.bands is ignored. Every fader can carry a cut
    (max_faders limits that if you want), and a howl only merges into a cut within 1/6 octave,
    so neighbouring faders can each take their own howl."""

    def __init__(self, freqs, config: NotchConfig | None = None, max_faders=None, clock=time.monotonic):
        super().__init__(config, clock)
        self.freqs = list(freqs)
        self.max_faders = max_faders or len(self.freqs)
        self.cfg.merge_oct = min(self.cfg.merge_oct, 1 / 6)

    def handle(self, freq_hz: float, severity_db: float = 0.0):
        now = self.clock()
        c = self.cfg
        near = self._nearest(freq_hz, c.merge_oct)
        if near is not None:
            return self._reshape(near, freq_hz, now, severity_db)
        if len(self.notches) >= self.max_faders:
            return "full", None
        idx = min(range(len(self.freqs)), key=lambda i: abs(math.log2(freq_hz / self.freqs[i])))
        if idx + 1 in self.notches:  # same fader, but outside merge range: can't happen with merge >= 1/3
            return self._reshape(self.notches[idx + 1], freq_hz, now, severity_db)
        n = Notch(band=idx + 1, freq_hz=freq_hz, gain_db=self._start_depth(severity_db),
                  width_oct=max(c.width_oct, 1 / 3), changed_at=now)
        self.notches[n.band] = n
        return "new", n


def geq_curve(notches, freqs, min_db=-12.0):
    """Spreads each cut over the graphic EQ as a bell: the fader nearest the howl gets the full
    depth, the next ones out get less. Overlapping cuts take the deeper value at each fader.
    Returns {fader number (1-based): gain dB} for every fader that should be cut (rounded to 0.5 dB)."""
    out = {}
    for n in notches:
        if n.band == HPF_BAND:
            continue
        sigma = max(0.2, n.width_oct * 0.75)  # in octaves
        centre = min(range(len(freqs)), key=lambda i: abs(math.log2(freqs[i] / n.freq_hz)))
        for i, f in enumerate(freqs):
            d = abs(math.log2(f / n.freq_hz))
            if d > 3 * sigma:
                continue
            # The nearest fader takes the full depth; the ones either side follow a bell.
            g = n.gain_db if i == centre else n.gain_db * math.exp(-0.5 * (d / sigma) ** 2)
            g = round(g * 2) / 2
            if g <= -1.0:
                out[i + 1] = max(min_db, min(out.get(i + 1, 0.0), g))
    return out
