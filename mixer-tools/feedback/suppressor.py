"""Glue: audio -> detector -> notch manager -> desk."""
from __future__ import annotations

import time

import numpy as np

from .detector import DetectorConfig, FeedbackDetector
from .notches import NotchConfig, NotchManager


class Suppressor:
    def __init__(self, desk, detector_cfg: DetectorConfig | None = None,
                 notch_cfg: NotchConfig | None = None, clock=time.monotonic, log=print):
        self.desk = desk
        self.detector = FeedbackDetector(detector_cfg)
        self.manager = NotchManager(notch_cfg, clock=clock)
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
            action, notch = self.manager.handle(ev.freq_hz)
            msg = (f"[howl] {ev.freq_hz:7.1f} Hz {ev.level_db:6.1f} dBFS  PAPR {ev.papr_db:4.1f}  "
                   f"PNPR {ev.pnpr_db:4.1f}  PHPR {ev.phpr_db:4.1f}  -> {action}")
            if notch is not None:
                msg += f" band {notch.band} {notch.freq_hz:.1f} Hz {notch.gain_db:+.1f} dB"
            self.log(msg)
            if action in ("new", "deeper"):
                self.desk.set_notch(notch)
            if action != "full":
                self.detector.forget(ev.freq_hz)

    def clear(self):
        for n in self.manager.clear():
            self.desk.clear_notch(n.band)
