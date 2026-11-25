import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import numpy as np

from feedback import synth
from feedback.desks import Dm7Desk, QuDesk, QuScale
from feedback.detector import FeedbackDetector
from feedback.notches import Notch, NotchConfig, NotchManager, octaves_to_q, q_to_octaves
from feedback.simloop import SimDesk, run_loop
from feedback.suppressor import Suppressor

SR = 48000


def detect(x, block=512):
    d = FeedbackDetector()
    out = []
    for i in range(0, len(x), block):
        out += d.process(x[i:i + block])
    return out


def rms_db(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12)


class DetectorTests(unittest.TestCase):
    def test_no_false_alarm_on_speech(self):
        for seed in range(4):
            self.assertEqual(detect(synth.speechlike(6, seed=seed)), [], f"seed {seed}")

    def test_no_false_alarm_on_harmonic_music_with_vibrato(self):
        self.assertEqual(detect(synth.music_chord(6)), [])
        self.assertEqual(detect(synth.music_chord(6, notes=(110.0, 164.8, 220.0, 329.6))), [])

    def test_finds_howl_under_speech(self):
        for f in (180.0, 850.0, 2500.0, 6300.0):
            evs = detect(synth.speechlike(5) + synth.howl(5, f))
            self.assertTrue(evs, f"missed {f} Hz")
            self.assertAlmostEqual(evs[0].freq_hz, f, delta=max(2.0, f * 0.002))
            # The howl starts at 1.0 s at -50 dBFS and grows 12 dB/s towards -6 dBFS.
            # It must be caught while it is still quiet, well before it gets loud.
            self.assertLess(evs[0].level_db, -25.0)

    def test_finds_howl_under_music(self):
        evs = detect(synth.music_chord(5) + synth.howl(5, 1234.0, start_db=-40))
        self.assertTrue(evs)
        self.assertAlmostEqual(evs[0].freq_hz, 1234.0, delta=3.0)

    def test_ignores_decaying_tone(self):
        # A struck note that rings and dies away is not feedback.
        t = np.arange(4 * SR) / SR
        x = 0.3 * np.exp(-t * 4) * np.sin(2 * np.pi * 1000 * t)
        self.assertEqual(detect(x), [])


class NotchTests(unittest.TestCase):
    def test_q_round_trip(self):
        for oct_ in (1 / 9, 1 / 3, 1.0):
            self.assertAlmostEqual(q_to_octaves(octaves_to_q(oct_)), oct_, places=6)
        self.assertAlmostEqual(octaves_to_q(1.0), 1.414, places=3)

    def test_new_deepen_max_full(self):
        now = [0.0]
        m = NotchManager(NotchConfig(bands=[1, 2], cooldown_s=0.5), clock=lambda: now[0])
        self.assertEqual(m.handle(1000.0)[0], "new")
        self.assertEqual(m.handle(1005.0)[0], "cooldown")
        now[0] = 1
        act, n = m.handle(1010.0)
        self.assertEqual((act, n.gain_db), ("deeper", -9.0))
        self.assertTrue(1000 < n.freq_hz < 1010)
        now[0] = 2; m.handle(1000.0)
        now[0] = 3
        self.assertEqual(m.handle(1000.0)[0], "max")
        self.assertEqual(m.handle(3000.0)[0], "new")
        self.assertEqual(m.handle(5000.0), ("full", None))


class DeskEncodingTests(unittest.TestCase):
    def test_qu_scale_end_points(self):
        s = QuScale()
        self.assertEqual((s.freq(20), s.freq(20000)), (0, 127))
        self.assertEqual((s.gain(-12), s.gain(0), s.gain(12)), (0, 64, 127))
        self.assertEqual((s.width(1.5), s.width(1 / 9)), (0, 127))

    def test_qu_calibration_overrides_curve(self):
        s = QuScale({"freq": [[0, 20], [64, 1000], [127, 20000]]})
        self.assertEqual(s.freq(1000), 64)

    def test_qu_nrpn_bytes(self):
        d = QuDesk(None, "Ip3", midi_channel=2)
        self.assertEqual(d.nrpn(0x0A, 0x50).hex(" "), "b1 63 22 b1 62 0a b1 06 50 b1 26 07")

    def test_dm7_lines(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            Dm7Desk(None, "InCh:4").set_notch(Notch(band=2, freq_hz=2500.0, gain_db=-6.0, width_oct=1 / 9))
        out = buf.getvalue()
        self.assertIn("set MIXER:Current/InCh/PEQ/Band/Freq 4 2 25000", out)
        self.assertIn("set MIXER:Current/InCh/PEQ/Band/Gain 4 2 -600", out)
        self.assertIn("set MIXER:Current/InCh/PEQ/Band/Q 4 2 12981", out)


class ClosedLoopTests(unittest.TestCase):
    def _run(self, freq, loop_gain_db, seconds=8):
        src = synth.speechlike(seconds, level=0.05)
        clock = [0.0]
        desk = SimDesk(SR)
        sup = Suppressor(desk, notch_cfg=NotchConfig(bands=[0, 1, 2, 3]), clock=lambda: clock[0], log=lambda *_: None)

        class Timed:
            def process(self, b):
                clock[0] += len(b) / SR
                sup.process(b)

        return src, run_loop(src, SR, Timed(), desk, howl_hz=freq, loop_gain_db=loop_gain_db), desk

    def test_loop_howls_without_control(self):
        src = synth.speechlike(6, level=0.05)
        mic = run_loop(src, SR, None, SimDesk(SR))
        self.assertGreater(rms_db(mic[-SR:]) - rms_db(src[-SR:]), 30)

    def test_suppressor_stops_the_howl(self):
        for freq, gain in ((2000.0, 1.0), (700.0, 2.0), (4500.0, 3.0)):
            src, mic, desk = self._run(freq, gain)
            self.assertTrue(desk.log, f"{freq} Hz: no notch placed")
            excess = rms_db(mic[-2 * SR:]) - rms_db(src[-2 * SR:])
            self.assertLess(excess, 3.0, f"{freq} Hz still howling (+{excess:.1f} dB), notches {desk.log}")


if __name__ == "__main__":
    unittest.main()
