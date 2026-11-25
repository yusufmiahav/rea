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
    last_ring: float = 0.0    # when this frequency last rang
    ceiling_db: float = 0.0   # recovery never lifts the cut above this (learned when it rang again)

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
    start_depth_db: float = -4.0
    step_db: float = -2.0
    max_depth_db: float = -12.0
    width_oct: float = 1 / 4       # starting width: a soft bell, not a pin-prick
    widen_step: float = 1.15       # each repeat ring widens the bell by this factor
    max_width_oct: float = 2 / 3
    total_cut_db: float = 18.0     # most cut allowed on one channel, all bands added together
    release_after_s: float = 8.0   # a cut that hasn't rung for this long starts to recover (0 = never)
    release_every_s: float = 2.0   # ...rising this often
    release_step_db: float = 1.0   # ...by this much, until it's flat and the band is free again
    merge_oct: float = 1 / 3       # a howl this close to a cut reshapes that cut, never a new band
    spill_oct: float = 2 / 3       # when every band is used, a howl this close widens the nearest cut
    lock_oct: float = 1 / 24       # within this of the centre the band stays put and just deepens
    cooldown_s: float = 1.5        # ignore re-hits on a cut for this long, so a change can take effect
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

    def total_cut(self):
        return -sum(n.gain_db for n in self.notches.values() if n.band != HPF_BAND)

    def _room(self):
        """dB of cut still allowed on this channel."""
        return max(0.0, self.cfg.total_cut_db - self.total_cut())

    def tick(self, now=None):
        """Lets cuts that have stopped ringing recover a little at a time. Returns a list of
        ("recover", Notch) for a cut that rose, and ("released", Notch) for one that is flat
        again (its band is free)."""
        c = self.cfg
        if c.release_after_s <= 0:
            return []
        now = self.clock() if now is None else now
        out = []
        for n in list(self.notches.values()):
            if n.band == HPF_BAND:
                continue
            if now - n.last_ring < c.release_after_s or now - n.changed_at < c.release_every_s:
                continue
            target = min(n.ceiling_db, n.gain_db + c.release_step_db)
            if target <= n.gain_db:
                continue
            n.gain_db, n.changed_at = target, now
            if n.gain_db > -0.5:
                del self.notches[n.band]
                out.append(("released", n))
            else:
                out.append(("recover", n))
        return out

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
        recovering = now - n.last_ring >= c.release_after_s > 0
        n.last_ring = now
        if recovering:
            # It rang again while recovering, so this level isn't safe: go back down a step at
            # once, and never lift it this far again.
            n.ceiling_db = min(n.ceiling_db, n.gain_db - 1.0)
            n.gain_db = max(c.max_depth_db, n.ceiling_db)
            n.hits += 1
            n.changed_at = now
            return "deeper", n
        if now - n.changed_at < c.cooldown_s:
            return "cooldown", n
        n.lo_hz, n.hi_hz = min(n.lo_hz, freq_hz), max(n.hi_hz, freq_hz)
        span = math.log2(n.hi_hz / n.lo_hz)
        room = self._room()
        if abs(math.log2(freq_hz / n.freq_hz)) <= c.lock_oct or span <= c.lock_oct:
            if n.gain_db <= c.max_depth_db and n.width_oct >= c.max_width_oct:
                return "max", n
            if room < 1.0 and n.gain_db > c.max_depth_db:
                return "budget", n
            n.gain_db = max(c.max_depth_db, n.gain_db - min(room, -c.step_db + extra / 2))
            n.width_oct = min(c.max_width_oct, n.width_oct * c.widen_step)
            action = "deeper"
        else:
            # Centre the bell between the lowest and highest howl and make it wide enough for both.
            n.freq_hz = math.sqrt(n.lo_hz * n.hi_hz)
            n.width_oct = min(c.max_width_oct, max(n.width_oct, span + c.width_oct))
            n.gain_db = max(c.max_depth_db, n.gain_db - min(room, (-c.step_db + extra / 2) / 2))
            action = "wider"
        n.hits += 1
        n.changed_at = now
        return action, n

    def _start_depth(self, severity_db):
        want = max(self.cfg.max_depth_db, self.cfg.start_depth_db - max(0.0, severity_db))
        return max(want, -self._room())

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
        if free and self._room() < 2.0:
            return "budget", None
        if free:
            n = Notch(last_ring=now, band=free[0], freq_hz=freq_hz, gain_db=self._start_depth(severity_db), width_oct=c.width_oct, changed_at=now)
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
        if self._room() < 2.0:
            return "budget", None
        n = Notch(last_ring=now, band=idx + 1, freq_hz=freq_hz, gain_db=self._start_depth(severity_db),
                  width_oct=max(c.width_oct, 1 / 3), changed_at=now)
        self.notches[n.band] = n
        return "new", n


GEQ_OFFSET = 100  # in a SplitManager, GEQ cuts are keyed 100 + fader number


class SplitManager:
    """Drives a channel's PEQ and GEQ together (Qu mixes and LR have both):
      * a severe howl, or one close to an existing PEQ cut, goes on the PEQ (narrow and precise),
      * a milder howl goes on the GEQ as a smooth curve,
      * a frequency is never cut on both, unless the PEQ cut there is already at its limit,
        when the GEQ adds a gentle supporting dip,
      * when one side is full or at its cut cap, the other takes over.
    The GEQ manager keys its cuts by fader number; to the outside (desk, saved file) they are
    GEQ_OFFSET + fader, so the desk can tell them apart from PEQ bands."""

    def __init__(self, peq: "NotchManager", geq: "GeqManager", severe_db=1.0):
        self.peq, self.geq, self.severe_db = peq, geq, severe_db
        self.managers = [peq, geq]
        self.clock = peq.clock

    @property
    def cfg(self):
        return self.peq.cfg

    @staticmethod
    def _out(n):
        return None if n is None else Notch(**{**n.__dict__, "band": GEQ_OFFSET + n.band})

    @property
    def notches(self):
        out = dict(self.peq.notches)
        out.update({GEQ_OFFSET + b: self._out(n) for b, n in self.geq.notches.items()})
        return out

    def _geq(self, freq_hz, severity_db):
        act, n = self.geq.handle(freq_hz, severity_db)
        return act, self._out(n)

    def handle(self, freq_hz: float, severity_db: float = 0.0):
        if self.peq._nearest(freq_hz, self.peq.cfg.merge_oct) is not None:
            act, n = self.peq.handle(freq_hz, severity_db)
            if act in ("max", "budget"):
                return self._geq(freq_hz, 0.0)         # PEQ is doing all it can here: GEQ supports
            return act, n
        if self.geq._nearest(freq_hz, self.geq.cfg.merge_oct) is not None:
            act, n = self._geq(freq_hz, severity_db)
            if act in ("max", "budget"):
                return self.peq.handle(freq_hz, severity_db)  # GEQ is at its limit: PEQ takes over
            return act, n
        if severity_db >= self.severe_db:
            act, n = self.peq.handle(freq_hz, severity_db)
            if act not in ("full", "budget"):
                return act, n
            return self._geq(freq_hz, severity_db)
        act, n = self._geq(freq_hz, severity_db)
        if act in ("full", "budget"):
            return self.peq.handle(freq_hz, severity_db)
        return act, n

    def tick(self, now=None):
        return list(self.peq.tick(now)) + [(a, self._out(n)) for a, n in self.geq.tick(now)]

    def clear(self, band=None):
        if band is None:
            return self.peq.clear() + [self._out(n) for n in self.geq.clear()]
        if band >= GEQ_OFFSET:
            return [self._out(n) for n in self.geq.clear(band - GEQ_OFFSET)]
        return self.peq.clear(band)

    def total_cut(self):
        return self.peq.total_cut() + self.geq.total_cut()

    def free_bands(self):
        return self.peq.free_bands()

    def to_json(self) -> str:
        items = [asdict(n) for n in self.peq.notches.values()] + [asdict(self._out(n)) for n in self.geq.notches.values()]
        return json.dumps(sorted(items, key=lambda d: d["band"]), indent=2)

    def load_json(self, text: str):
        items = json.loads(text)
        self.peq.notches = {d["band"]: Notch(**d) for d in items if d["band"] < GEQ_OFFSET}
        self.geq.notches = {d["band"] - GEQ_OFFSET: Notch(**{**d, "band": d["band"] - GEQ_OFFSET})
                            for d in items if d["band"] >= GEQ_OFFSET}


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
            out[i + 1] = max(min_db, min(out.get(i + 1, 0.0), g))
    return smooth_geq(out, len(freqs))


def smooth_geq(cuts: dict, n_faders: int, max_step_db=4.0, bridge=2) -> dict:
    """Makes the faders one continuous curve instead of a zig-zag:
      * a fader up to `bridge` places from deeper faders on both sides is brought down onto the line
        between them, so two nearby cuts join up instead of leaving a spike between them,
      * neighbouring faders never differ by more than `max_step_db` (the shallower one comes down),
    then rounds to 0.5 dB and drops anything shallower than 1 dB."""
    g = [cuts.get(i + 1, 0.0) for i in range(n_faders)]
    for _ in range(2):
        for i in range(n_faders):
            left = [(i - k, g[i - k]) for k in range(1, bridge + 1) if i - k >= 0 and g[i - k] < g[i]]
            right = [(i + k, g[i + k]) for k in range(1, bridge + 1) if i + k < n_faders and g[i + k] < g[i]]
            if left and right:
                (li, lg), (ri, rg) = min(left, key=lambda t: t[1]), min(right, key=lambda t: t[1])
                line = lg + (rg - lg) * (i - li) / (ri - li)
                g[i] = min(g[i], line)
    for i in range(1, n_faders):                       # limit the slope, left to right...
        g[i] = min(g[i], g[i - 1] + max_step_db)
    for i in range(n_faders - 2, -1, -1):              # ...and right to left
        g[i] = min(g[i], g[i + 1] + max_step_db)
    out = {}
    for i, v in enumerate(g):
        v = round(v * 2) / 2
        if v <= -1.0:
            out[i + 1] = v
    return out
