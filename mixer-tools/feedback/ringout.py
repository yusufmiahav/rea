"""Ring-out: bring a fader up slowly and let the suppressor notch each frequency that
starts to ring, until the fader reaches its limit.

Safety:
  * the fader never goes above max_db,
  * every howl pulls the fader straight back by backoff_db, then it waits settle_s
    before climbing again,
  * if the input peak goes over guard_dbfs, the fader drops by guard_drop_db and the
    ring-out stops,
  * it stops when the EQ runs out of bands (or a notch is already at full depth),
  * stop() (and finishing) puts the fader back to the level it started from.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RingOutConfig:
    start_db: float = -30.0
    max_db: float = 0.0
    step_db: float = 1.0
    step_s: float = 2.0
    backoff_db: float = 4.0
    settle_s: float = 3.0
    guard_dbfs: float = -3.0
    guard_drop_db: float = 10.0
    restore_on_finish: bool = True


class RingOut:
    def __init__(self, desk, config: RingOutConfig | None = None, clock=time.monotonic, log=print):
        self.desk = desk
        self.cfg = config or RingOutConfig()
        self.clock = clock
        self.log = log
        self.running = False
        self.level = self.cfg.start_db
        self.best_stable = None   # highest level reached without a howl
        self.howl_levels = []
        self.result = None
        self._next = 0.0

    def start(self):
        c = self.cfg
        if c.max_db <= c.start_db:
            raise ValueError("the max fader level must be above the start level")
        self.level = c.start_db
        self.desk.set_fader(self.level)
        self.running = True
        self.result = None
        self._next = self.clock() + c.step_s
        self.log(f"[ring-out] {self.desk.target}: fader to {self.level:+.1f} dB, climbing "
                 f"{c.step_db:g} dB every {c.step_s:g} s up to {c.max_db:+.1f} dB")

    def tick(self, peak_dbfs: float):
        if not self.running:
            return
        c = self.cfg
        now = self.clock()
        if peak_dbfs > c.guard_dbfs:
            self.level -= c.guard_drop_db
            self.desk.set_fader(self.level)
            self._finish(f"input reached {peak_dbfs:.1f} dBFS (limit {c.guard_dbfs:g}); fader pulled down",
                         restore=False)
            return
        if now < self._next:
            return
        if self.best_stable is None or self.level > self.best_stable:
            self.best_stable = self.level
        if self.level >= c.max_db:
            self._finish(f"reached the max level {c.max_db:+.1f} dB")
            return
        self.level = min(c.max_db, self.level + c.step_db)
        self.desk.set_fader(self.level)
        self._next = now + c.step_s

    def on_howl(self, action, freq_hz):
        if not self.running:
            return
        c = self.cfg
        self.howl_levels.append((self.level, freq_hz))
        self.level = max(c.start_db - c.guard_drop_db, self.level - c.backoff_db)
        self.desk.set_fader(self.level)
        self._next = self.clock() + c.settle_s
        self.log(f"[ring-out] howl at {freq_hz:.0f} Hz -> fader back to {self.level:+.1f} dB, settling")
        if action in ("full", "max"):
            self._finish("no EQ left for this howl (all bands used or at full depth)")

    def stop(self):
        if self.running:
            self._finish("stopped")

    def _finish(self, why, restore=None):
        self.running = False
        restore = self.cfg.restore_on_finish if restore is None else restore
        stable = f"{self.best_stable:+.1f} dB" if self.best_stable is not None else "n/a"
        self.result = f"{why}. Highest level held without howling: {stable}"
        if restore:
            self.level = self.cfg.start_db
            self.desk.set_fader(self.level)
            self.result += f". Fader returned to {self.level:+.1f} dB"
        self.log(f"[ring-out] {self.desk.target}: {self.result}")
