"""Mixer page back-ends: addresses and values checked against the examples printed in the vendor docs."""
import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feedback import mixer as mx  # noqa: E402
from feedback.desks import QuDesk  # noqa: E402


def hexs(b):
    return b.hex(" ").upper()


class SqTests(unittest.TestCase):
    def setUp(self):
        self.lines = []
        self.c = mx.MidiTcp(None, apply=False, log=self.lines.append, tag="sq")
        self.m = mx.SqMixer(self.c, "Allen & Heath SQ-5 / SQ-6 / SQ-7")

    def addr(self, a):
        return "%02X %02X" % mx.SqAddress.split(a)

    def test_doc_addresses(self):
        m = self.m
        self.assertEqual(self.addr(m.fader_addr("Ip1")), "40 00")
        self.assertEqual(self.addr(m.fader_addr("Grp1")), "40 30")
        self.assertEqual(self.addr(m.fader_addr("FXRtn1")), "40 3C")
        self.assertEqual(self.addr(m.send_addr("Ip1", "Aux1")), "40 44")
        self.assertEqual(self.addr(m.send_addr("Ip1", "Aux12")), "40 4F")
        self.assertEqual(self.addr(m.send_addr("Ip48", "Aux1")), "44 78")
        self.assertEqual(self.addr(m.send_addr("Ip48", "Aux12")), "45 03")
        self.assertEqual(self.addr(m.send_addr("Grp1", "Aux1")), "45 04")
        self.assertEqual(self.addr(m.send_addr("FXRtn1", "Aux1")), "46 14")
        self.assertEqual(self.addr(m.send_addr("Ip1", "FX1")), "4C 14")
        self.assertEqual(self.addr(m.send_addr("Ip1", "FX4")), "4C 17")
        self.assertEqual(self.addr(m.send_addr("Ip24", "Aux5") + mx.SqAddress.PAN_OFFSET), "52 5C")
        self.assertEqual(self.addr(m.send_addr("Ip1", "Aux1") + mx.SqAddress.ASSIGN_OFFSET), "60 44")
        self.assertEqual(self.addr(mx.SqAddress.GROUP_ASSIGN), "66 74")
        self.assertEqual(self.addr(mx.SqAddress.GROUP_ASSIGN + 1 * 12), "67 00")
        self.assertEqual(self.addr(mx.SqAddress.GROUP_ASSIGN + 22 * 12), "68 7C")
        for t, a in (("LR", "4F 00"), ("Aux1", "4F 01"), ("Aux12", "4F 0C"), ("FX1", "4F 0D"), ("FX4", "4F 10"),
                     ("Mtx1", "4F 11"), ("Mtx3", "4F 13"), ("DCA1", "4F 20"), ("DCA8", "4F 27")):
            self.assertEqual(self.addr(m.fader_addr(t)), a, t)
        for t, a in (("Ip1", "00 00"), ("Ip48", "00 2F"), ("Grp1", "00 30"), ("FXRtn8", "00 43"), ("LR", "00 44"),
                     ("Aux1", "00 45"), ("FX4", "00 54"), ("Mtx3", "00 57"), ("DCA1", "02 00")):
            self.assertEqual(self.addr(m.mute_addr(t)), a, t)

    def test_levels(self):
        for db, vcvf in ((10, 0x7F7F), (0, 0x765C), (-10, 0x6D39), (-20, 0x6416), (-30, 0x5A72), (-45, 0x4C7D)):
            v = mx.sq_level_value(db)
            self.assertLessEqual(abs(v - ((vcvf >> 8) << 7 | (vcvf & 0xFF))), 2, db)
            self.assertAlmostEqual(mx.sq_level_db(v), db, delta=0.05)
        self.assertEqual(mx.sq_level_value(-90), 0)
        self.assertEqual(mx.sq_level_value(0, "audio"), 0x62 << 7)

    def test_messages_and_readback(self):
        self.m.mute("Ip1", True)
        self.assertIn("B0 63 00 B0 62 00 B0 06 00 B0 26 01", self.lines[-1])
        self.m.fader("LR", 0.0)
        self.assertIn("B0 63 4F B0 62 00 B0 06 76 B0 26 5C", self.lines[-1])
        self.m.pan("Ip1", 0.0)
        self.assertIn("B0 06 3F B0 26 7F", self.lines[-1])
        # the desk reports values the same way
        self.c.feed(bytes([0xB0, 0x63, 0x40, 0xB0, 0x62, 0x00, 0xB0, 0x06, 0x76, 0xB0, 0x26, 0x5C,
                           0xB0, 0x63, 0x00, 0x62, 0x00, 0x06, 0x00, 0x26, 0x01]))
        st = self.m.state("Ip1")
        self.assertAlmostEqual(st["fader"], 0.0, delta=0.05)
        self.assertTrue(st["mute"])

    def test_qu567(self):
        m = mx.SqMixer(self.c, "Allen & Heath Qu-5 / Qu-6 / Qu-7")
        self.assertEqual(self.addr(m.fader_addr("ST1")), "40 20")
        self.assertEqual(self.addr(m.fader_addr("ST2")), "40 22")
        self.assertEqual(self.addr(m.fader_addr("USB")), "40 24")
        self.assertEqual(m.label("Aux3"), "Mix3")
        self.assertEqual(len(m.targets()), 32 + 3 + 6 + 12 + 12 + 4 + 3 + 8 + 1)


class AhTests(unittest.TestCase):
    def test_dlive_messages(self):
        lines = []
        c = mx.MidiTcp(None, apply=False, log=lines.append, tag="dlive")
        m = mx.AhTcpMixer(c, "Allen & Heath dLive", base_channel=1)
        m.mute("Ip1", True)
        self.assertIn("90 00 7F 90 00 00", lines[-1])
        m.fader("Ip1", 0.0)
        self.assertIn("B0 63 00 B0 62 17 B0 06 6B", lines[-1])
        m.fader("DCA1", -90)
        self.assertIn("B4 63 36 B4 62 17 B4 06 00", lines[-1])
        m.send_level("Ip2", "Aux1", 0.0)
        self.assertIn("F0 00 00 1A 50 10 01 00 00 0D 01 02 00 6B F7", lines[-1])
        # names, colours and faders reported by the desk
        c.feed(mx.AH_TCP_HDR + bytes([0x00, 0x02, 0x00]) + b"Kick" + b"\xF7")
        c.feed(mx.AH_TCP_HDR + bytes([0x00, 0x05, 0x00, 0x01, 0xF7]))
        c.feed(bytes([0xB0, 0x63, 0x00, 0xB0, 0x62, 0x17, 0xB0, 0x06, 0x6B, 0x90, 0x00, 0x7F]))
        self.assertEqual(m.label("Ip1"), "Kick")
        self.assertEqual(m.colour("Ip1"), "#c0392b")
        self.assertAlmostEqual(m.state("Ip1")["fader"], 0.0, delta=0.3)
        self.assertTrue(m.state("Ip1")["mute"])

    def test_avantis_counts(self):
        m = mx.AhTcpMixer(mx.MidiTcp(None), "Allen & Heath Avantis", base_channel=12)
        self.assertEqual(m.addr("Ip96"), (11, 0x5F))
        self.assertEqual(m.addr("DCA16"), ((11 + 4) & 0x0F, 0x45))
        with self.assertRaises(ValueError):
            m.addr("Ip97")


class QuTests(unittest.TestCase):
    def test_qu_mixer(self):
        lines = []
        d = QuDesk(None, "Ip1", apply=False, log=lines.append)
        m = mx.QuMixer(d, "Allen & Heath Qu-24")
        self.assertIn(("Groups / Mtx", ["Grp1-2", "Grp3-4", "Grp5-6", "Grp7-8", "Mtx1-2", "Mtx3-4"]), m.layers())
        m.fader("DCA1", 0.0)
        self.assertIn("B0 63 10 B0 62 17 B0 06 62 B0 26 07", lines[-1])
        m.send_level("Ip1", "FX1", 0.0)
        self.assertIn("B0 63 20 B0 62 20 B0 06 62 B0 26 10", lines[-1])
        m.group_assign("Ip1", "Grp1-2", True)
        self.assertIn("B0 63 20 B0 62 55 B0 06 01 B0 26 08", lines[-1])
        d.params.feed(bytes([0xF0, 0, 0, 0x1A, 0x50, 0x11, 1, 0, 0, 2, 0x20]) + b"Vox" + b"\xF7" +
                      bytes([0xB0, 0x63, 0x20, 0x62, 0x17, 0x06, 0x62, 0x26, 0x07, 0x90, 0x20, 0x7F]))
        self.assertEqual(m.label("Ip1"), "Vox")
        self.assertAlmostEqual(m.state("Ip1")["fader"], 0.0)
        self.assertTrue(m.state("Ip1")["mute"])
        self.assertNotIn("Grp1-2", mx.QuMixer(d, "Allen & Heath Qu-16").targets())


class FakeRcp(threading.Thread):
    """A tiny RCP desk: 6 input channels, 2 mixes; answers get/set and sends NOTIFY."""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen()
        self.port = self.srv.getsockname()[1]
        self.counts = {"InCh": 6, "Mix": 2, "Mtrx": 1, "St": 1, "DCA": 2}
        self.vals = {("MIXER:Current/InCh/Label/Name", 0, 0): '"Kick"'}

    def run(self):
        c, _ = self.srv.accept()
        f = c.makefile("rb")
        for raw in f:
            line = raw.decode().strip()
            tok = mx.rcp_tokens(line)
            if tok[0] == "get" and len(tok) >= 4:
                path, x, y = tok[1], int(tok[2]), int(tok[3])
                kind = path.split("/")[1]
                if x >= self.counts.get(kind, 0):
                    c.sendall(f"ERROR get {path} {x} {y} InvalidArgument\n".encode())
                    continue
                v = self.vals.get((path, x, y), "0")
                c.sendall(f"OK get {path} {x} {y} {v}\n".encode())
            elif tok[0] == "set":
                c.sendall(f"OK {line}\n".encode())
            elif tok[0] == "devinfo":
                c.sendall(b'OK devinfo productname "DM7"\n')
            else:
                c.sendall(f"OK {line}\n".encode())
            if line == "get MIXER:Current/InCh/ToSt/On 1 0":
                c.sendall(b'NOTIFY set MIXER:Current/InCh/Fader/Level 1 0 -1000 "-10.00"\n')


class RcpTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(mx.rcp_parse('OK get MIXER:Current/InCh/Fader/Level 0 0 -1000'),
                         ("MIXER:Current/InCh/Fader/Level", 0, 0, -1000))
        self.assertEqual(mx.rcp_parse('NOTIFY set MIXER:Current/InCh/Label/Name 3 0 "Lead Vox"'),
                         ("MIXER:Current/InCh/Label/Name", 3, 0, "Lead Vox"))
        self.assertIsNone(mx.rcp_parse("ERROR get MIXER:Current/InCh/Fader/Level 999 0 x"))

    def test_fake_desk(self):
        srv = FakeRcp()
        srv.start()
        orig = socket.create_connection
        socket.create_connection = lambda addr, timeout=None: orig(("127.0.0.1", srv.port), timeout)
        try:
            lines = []
            m, conn = mx.make_mixer("Yamaha DM7", "127.0.0.1", True, lines.append)
        finally:
            socket.create_connection = orig
        try:
            self.assertEqual(m.counts["InCh"], 6)
            self.assertEqual(m.counts["Mix"], 2)
            m.refresh(m.targets()[:2])
            end = time.time() + 3
            while time.time() < end and m.label("InCh:0") != "Kick":
                time.sleep(0.05)
            self.assertEqual(m.label("InCh:0"), "Kick")
            while time.time() < end and m.state("InCh:1").get("fader") != -10.0:
                time.sleep(0.05)
            self.assertAlmostEqual(m.state("InCh:1")["fader"], -10.0)
            m.mute("InCh:0", True)
            self.assertIn("set MIXER:Current/InCh/Fader/On 0 0 0", lines[-1])
            self.assertTrue(m.state("InCh:0")["mute"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
