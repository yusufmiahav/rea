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
        m = NotchManager(NotchConfig(bands=[2, 3], cooldown_s=0.5), clock=lambda: now[0])
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

    def test_qu_bands_are_numbered_like_the_desk(self):
        out = []
        d = QuDesk(None, "Ip1", log=out.append)
        d.set_notch(Notch(band=3, freq_hz=2500.0, gain_db=-6.0, width_oct=1 / 9))  # 3 = HM
        self.assertIn("PEQ HM", out[0])
        self.assertIn("B0 62 0A", out[0])  # HM freq ID
        QuDesk.check_bands([1, 2, 3, 4])
        with self.assertRaises(ValueError):
            QuDesk.check_bands([2, 3, 4, 5])
        with self.assertRaises(ValueError):
            QuDesk.check_bands([0, 1])

    def test_qu_geq_on_mix(self):
        from feedback.desks import QuGeqDesk
        out = []
        d = QuGeqDesk(None, "Mix1", log=out.append)
        d.set_notch(Notch(band=20, freq_hz=2500.0, gain_db=-6.0, width_oct=1 / 3))
        self.assertIn("B0 63 60 B0 62 70 B0 06 20 B0 26 13", out[0])  # Mix1, GEQ gain, -6 dB, fader 20 (VX 13)
        self.assertIn("B0 62 71 B0 06 01 B0 26 00", out[0])  # GEQ in
        with self.assertRaises(ValueError):
            QuGeqDesk(None, "Ip1")

    def test_geq_manager_picks_nearest_fader(self):
        from feedback.desks import QU_GEQ_FREQS
        from feedback.notches import GeqManager
        now = [0.0]
        m = GeqManager(QU_GEQ_FREQS, NotchConfig(), max_faders=2, clock=lambda: now[0])
        act, n = m.handle(2600.0)
        self.assertEqual((act, n.band, n.freq_hz), ("new", 20, 2500))
        now[0] = 1
        self.assertEqual(m.handle(2450.0)[0], "deeper")
        self.assertEqual(m.handle(108.9)[1].band, 6)  # 100 Hz fader
        self.assertEqual(m.handle(5000.0), ("full", None))

    def test_qu_link_check_reads_box_and_midi_channel(self):
        import socket, threading
        from feedback.desks import AH_HDR
        a, b = socket.socketpair()
        d = QuDesk(None, "Ip1", log=lambda *_: None)
        d.apply, d.sock = True, a
        threading.Thread(target=d._drain, daemon=True).start()

        def desk():
            req = b.recv(64)
            assert req == AH_HDR + bytes([0x7F, 0x10, 0x00, 0xF7]), req
            b.sendall(b"\xFE" + AH_HDR + bytes([0x02, 0x11, 0x01, 0x01, 0x09, 0xF7]))
        threading.Thread(target=desk, daemon=True).start()
        self.assertEqual(d.check_link(), "Qu-16 firmware 1.9, MIDI channel 3")
        self.assertEqual(d.nrpn(0x0A, 0)[0], 0xB2)  # now talks on the desk's channel
        d.stop.set(); a.close(); b.close()

    def test_dm7_lines(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            Dm7Desk(None, "InCh:4").set_notch(Notch(band=3, freq_hz=2500.0, gain_db=-6.0, width_oct=1 / 9))
        out = buf.getvalue()
        self.assertIn("set MIXER:Current/InCh/PEQ/Band/Freq 4 2 25000", out)
        self.assertIn("set MIXER:Current/InCh/PEQ/Band/Gain 4 2 -600", out)
        self.assertIn("set MIXER:Current/InCh/PEQ/Band/Q 4 2 12981", out)


class ClosedLoopTests(unittest.TestCase):
    def _run(self, freq, loop_gain_db, seconds=8):
        src = synth.speechlike(seconds, level=0.05)
        clock = [0.0]
        desk = SimDesk(SR)
        sup = Suppressor(desk, notch_cfg=NotchConfig(bands=[1, 2, 3, 4]), clock=lambda: clock[0], log=lambda *_: None)

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


class MultiChannelAndRingOutTests(unittest.TestCase):
    def test_views_share_one_qu_connection(self):
        out = []
        d = QuDesk(None, "Ip1", log=out.append)
        v = d.view("Mix2")
        self.assertIs(v.sock, d.sock)
        v.set_notch(Notch(band=2, freq_hz=500.0, gain_db=-6.0, width_oct=1 / 9))
        self.assertIn("Mix2 PEQ LM", out[-1])
        self.assertIn("B0 63 61", out[-1])  # Mix2 channel code
        d.status = 0xB4  # parent learns the desk's MIDI channel later
        self.assertEqual(v.nrpn(0x17, 0)[0], 0xB4)
        v.close()  # a view must not close the shared socket
        self.assertFalse(d.stop.is_set())

    def test_qu_fader_values(self):
        from feedback.desks import qu_fader_value
        self.assertEqual(qu_fader_value(0), 0x62)
        self.assertEqual(qu_fader_value(10), 0x7F)
        self.assertEqual(qu_fader_value(-10), 0x3F)
        self.assertEqual(qu_fader_value(-200), 0)

    def test_dm7_fader_line(self):
        out = []
        d = Dm7Desk(None, "Mix:3", log=out.append).view("InCh:5")
        d.set_fader(-6.5)
        self.assertEqual(out[-1], "[dm7 dry-run] set MIXER:Current/InCh/Fader/Level 5 0 -650")

    def _ringout(self, cfg, loop_gain_db=1.0, seconds=20):
        from feedback.ringout import RingOut
        clock = [0.0]
        desk = SimDesk(SR)
        ro = RingOut(desk, cfg, clock=lambda: clock[0], log=lambda *_: None)
        sup = Suppressor(desk, notch_cfg=NotchConfig(bands=[1, 2, 3, 4]), clock=lambda: clock[0],
                         log=lambda *_: None, on_event=lambda a, e: ro.on_howl(a, e.freq_hz))

        class Timed:
            def process(self, b):
                clock[0] += len(b) / SR
                sup.process(b)

        ro.start()
        run_loop(synth.speechlike(seconds, level=0.05), SR, Timed(), desk, loop_gain_db=loop_gain_db,
                 on_block=lambda m: ro.tick(20 * np.log10(np.abs(m).max() + 1e-9)))
        return ro, desk

    def test_ring_out_notches_and_reaches_max(self):
        from feedback.ringout import RingOutConfig
        ro, desk = self._ringout(RingOutConfig(start_db=-12, max_db=3, step_s=0.4, settle_s=1.0))
        self.assertFalse(ro.running)
        self.assertTrue(desk.log, "no notch placed")
        self.assertIn("reached the max level", ro.result)
        self.assertLessEqual(max(desk.fader_log), 3.0)  # never above the limit
        self.assertEqual(desk.fader_db, -12)            # returned to the start level

    def test_ring_out_level_guard(self):
        from feedback.ringout import RingOut, RingOutConfig
        desk = SimDesk(SR)
        ro = RingOut(desk, RingOutConfig(start_db=-20, max_db=0, guard_dbfs=-3), clock=lambda: 0.0, log=lambda *_: None)
        ro.start()
        ro.tick(-1.0)
        self.assertFalse(ro.running)
        self.assertEqual(desk.fader_db, -30)  # pulled down 10 dB, not restored upward
        self.assertIn("input reached", ro.result)

    def test_ring_out_stop_restores(self):
        from feedback.ringout import RingOut, RingOutConfig
        t = [0.0]
        desk = SimDesk(SR)
        ro = RingOut(desk, RingOutConfig(start_db=-20, max_db=0, step_s=1), clock=lambda: t[0], log=lambda *_: None)
        ro.start()
        for _ in range(5):
            t[0] += 1.01
            ro.tick(-40)
        self.assertGreater(desk.fader_db, -20)
        ro.stop()
        self.assertEqual(desk.fader_db, -20)
