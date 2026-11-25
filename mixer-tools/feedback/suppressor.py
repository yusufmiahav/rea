"""Glue: audio -> detector -> notch manager -> desk."""
from __future__ import annotations

import time

import numpy as np

from .detector import DetectorConfig, FeedbackDetector
from .notches import NotchConfig, NotchManager


class Suppressor:
    def __init__(self, desk, detector_cfg: DetectorConfig | None = None,
                 notch_cfg: NotchConfig | None = None, clock=time.monotonic, log=print, manager=None, on_event=None):
        self.desk = desk
        self.on_event = on_event  # called as on_event(action, FeedbackEvent) after each howl
        self.detector = FeedbackDetector(detector_cfg)
        self.manager = manager or NotchManager(notch_cfg, clock=clock)
        self.log = log
        self.frozen = False   # True = keep listening, change nothing (show mode)
        self.history = []

    def process(self, block: np.ndarray):
        for ev in self.detector.process(block):
            self.history.append(ev)
            if self.frozen:
                self.log(f"[howl] {ev.freq_hz:7.1f} Hz {ev.level_db:6.1f} dBFS (frozen, no change)")
                self.detector.forget(ev.freq_hz)
                continue
            action, notch = self.manager.handle(ev.freq_hz, self.severity(ev))
            msg = (f"[howl] {ev.freq_hz:7.1f} Hz {ev.level_db:6.1f} dBFS  PAPR {ev.papr_db:4.1f}  "
                   f"PNPR {ev.pnpr_db:4.1f}  PHPR {ev.phpr_db:4.1f}  -> {action}")
            if notch is not None and notch.band == 0:
                msg += f" HPF {notch.freq_hz:.0f} Hz"
            elif notch is not None:
                msg += f" band {notch.band} {notch.freq_hz:.1f} Hz {notch.gain_db:+.1f} dB {notch.width_oct:.2f} oct"
            self.log(msg)
            if action in ("new", "deeper", "wider", "hpf"):
                self.desk.set_notch(notch)
            if action != "full":
                self.detector.forget(ev.freq_hz)
            if self.on_event:
                self.on_event(action, ev)
        if not self.frozen:
            for action, n in self.manager.tick():
                if action == "released":
                    self.log(f"[recover] {n.freq_hz:7.1f} Hz cut is flat again, band {n.band} is free")
                    self.desk.clear_notch(n.band)
                else:
                    self.log(f"[recover] {n.freq_hz:7.1f} Hz cut eased to {n.gain_db:+.1f} dB")
                    self.desk.set_notch(n)

    def severity(self, ev):
        """Extra cut depth in dB (0-3) for a worse howl: one that stands further out of the
        spectrum than the detection threshold, or is already loud."""
        over = 0.25 * max(0.0, ev.papr_db - self.detector.cfg.papr_db)
        loud = 0.15 * max(0.0, ev.level_db + 30.0)
        return min(3.0, round(over + loud, 1))

    def clear(self):
        for n in self.manager.clear():
            self.desk.clear_notch(n.band)
