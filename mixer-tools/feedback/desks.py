"""Desk back-ends that write a notch to a channel's parametric EQ.

Every back-end has the same three calls: `set_notch(notch)`, `clear_notch(band)` and
`close()`. Nothing is sent unless you build a real back-end with apply=True; with
apply=False each one just prints the exact bytes or lines it would send.

Sources for the message formats (public vendor docs):
  * Allen & Heath "Qu MIDI Protocol V1.9": NRPN on TCP 51325,
    BN 63 CH  BN 62 ID  BN 06 VA  BN 26 07 for every PEQ parameter.
    Gain 00..7F = -12..+12 dB (40 = 0 dB), Freq 00..7F = 20 Hz..20 kHz,
    Width 00..7F = 1.5 oct..1/9 oct, LF/HF type Bell = 00, PEQ in = ID 11 VA 01 VX 00.
    The doc gives only the end points. The curve in between is ASSUMED here
    (logarithmic for freq and width, linear for gain). Check it with
    `qu16_probe.py sync`, then put the real points in a calibration file.
  * Yamaha "DM7 OSC Specifications V1.1.0" (the same parameter tree is used by RCP on
    TCP 49280): MIXER:Current/InCh/PEQ/Band/{Freq,Gain,Q,Bypass} X=channel Y=band,
    Freq 200..200000 (0.1 Hz), Gain -1800..1800 (taken as 0.01 dB), Q 100..16000 (0.001).
    Check these with `dm7_rcp_probe.py get` before using apply=True.
"""
from __future__ import annotations

import json
import math
import socket
import threading
import time

from .notches import Notch, octaves_to_q


class DryRun:
    name = "dry-run"

    def __init__(self, target="(none)", log=print):
        self.target = target
        self.log = log

    @staticmethod
    def check_bands(bands):
        pass

    def check_link(self, timeout=3.0):
        return "dry run"

    def set_notch(self, n: Notch):
        self.log(f"[dry-run] {self.target} band {n.band}: {n.freq_hz:7.1f} Hz  {n.gain_db:+.1f} dB  {n.width_oct:.3f} oct")

    def clear_notch(self, band):
        self.log(f"[dry-run] {self.target} band {band}: flat")

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Allen & Heath Qu-16 / 24 / 32 / Pac / SB
# ---------------------------------------------------------------------------
QU_BANDS = {  # band number as shown on the desk -> (gain ID, freq ID, width ID, type ID or None)
    1: (0x01, 0x02, 0x03, 0x04),  # LF (set to bell when used)
    2: (0x05, 0x06, 0x07, None),  # LM
    3: (0x09, 0x0A, 0x0B, None),  # HM
    4: (0x0D, 0x0E, 0x0F, 0x10),  # HF (set to bell when used)
}
QU_BAND_NAMES = {1: "LF", 2: "LM", 3: "HM", 4: "HF"}
# Qu GEQ: 28 faders, VX 00..1B, per the Qu MIDI Protocol V1.9 reference table.
QU_GEQ_FREQS = [31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250,
                1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000]
QU_GEQ_GAIN = 0x70
QU_GEQ_IN = 0x71
AH_HDR = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00])
QU_BOX_IDS = {1: "Qu-16", 2: "Qu-24", 3: "Qu-32", 4: "Qu-Pac", 5: "Qu-SB"}
QU_PEQ_IN = 0x11
QU_PEQ_VX = 0x07


def qu_channel_code(target: str) -> int:
    """'Ip1'..'Ip32', 'ST1'..'ST3', 'Mix1'..'Mix10' (mono mixes 1-4), 'LR', or a hex code like '0x20'."""
    t = target.strip()
    low = t.lower()
    if low.startswith("0x"):
        return int(t, 16)
    if low.startswith("ip"):
        n = int(t[2:]); assert 1 <= n <= 32; return 0x1F + n
    if low.startswith("st"):
        n = int(t[2:]); assert 1 <= n <= 3; return 0x3F + n
    if low.startswith("mix"):
        n = int(t[3:]); assert 1 <= n <= 4, "Qu mono mixes are Mix1-4; use 0x64-0x66 for 5-6..9-10"; return 0x5F + n
    if low == "lr":
        return 0x67
    raise ValueError(f"unknown Qu target {target!r}")


class QuScale:
    """Maps real units to Qu 7-bit values. Default curves are assumptions; a calibration
    file overrides them with measured points: {"freq": [[va, hz], ...], "width": [[va, oct], ...]}."""

    def __init__(self, calibration: dict | None = None):
        self.cal = calibration or {}

    @staticmethod
    def _interp(points, x, log_x=False):
        pts = sorted(points, key=lambda p: p[1])
        xs = [math.log(p[1]) if log_x else p[1] for p in pts]
        xv = math.log(x) if log_x else x
        if xv <= xs[0]:
            return pts[0][0]
        for (v0, _), (v1, _), a, b in zip(pts, pts[1:], xs, xs[1:]):
            if a <= xv <= b:
                return round(v0 + (v1 - v0) * (xv - a) / (b - a))
        return pts[-1][0]

    def freq(self, hz: float) -> int:
        if "freq" in self.cal:
            return self._interp(self.cal["freq"], hz, log_x=True)
        hz = min(20000.0, max(20.0, hz))
        return round(127 * math.log(hz / 20.0) / math.log(1000.0))

    def width(self, octaves: float) -> int:
        if "width" in self.cal:
            return self._interp(self.cal["width"], octaves, log_x=True)
        octaves = min(1.5, max(1 / 9, octaves))
        return round(127 * math.log(1.5 / octaves) / math.log(1.5 * 9))

    @staticmethod
    def gain(db: float) -> int:
        return max(0, min(127, round(64 + db / 0.1875)))


class QuDesk:
    name = "qu"
    PORT = 51325

    def __init__(self, host, target, midi_channel=1, apply=False, calibration=None, log=print):
        self.ch = qu_channel_code(target)
        self.target = target
        self.status = 0xB0 | ((midi_channel - 1) & 0x0F)
        self.scale = QuScale(calibration)
        self.apply = apply
        self.log = log
        self.sock = None
        self.stop = threading.Event()
        self.box = None
        self.desk_midi_channel = None
        self._state_reply = threading.Event()
        self._rx = b""
        if apply:
            self.sock = socket.create_connection((host, self.PORT), timeout=5)
            self.sock.settimeout(None)
            threading.Thread(target=self._keepalive, daemon=True).start()
            threading.Thread(target=self._drain, daemon=True).start()

    @staticmethod
    def check_bands(bands):
        bad = [b for b in bands if b not in QU_BANDS]
        if bad:
            raise ValueError(f"Qu PEQ bands are 1-4 (1=LF 2=LM 3=HM 4=HF); got {bad}")

    def check_link(self, timeout=3.0):
        """Ask the desk for its system state. Returns e.g. 'Qu-16 firmware 1.9.5', or raises."""
        if not self.apply:
            return "dry run (not connected)"
        self._state_reply.clear()
        self.sock.sendall(AH_HDR + bytes([0x7F, 0x10, 0x00, 0xF7]))
        if not self._state_reply.wait(timeout):
            raise ConnectionError("the desk did not answer on TCP 51325. Check the IP, and that no other "
                                  "MIDI-over-TCP app (A&H MIDI Control, a DAW link) is connected")
        if self.desk_midi_channel:
            return f"{self.box}, MIDI channel {self.desk_midi_channel}"
        return self.box

    def _keepalive(self):
        while not self.stop.is_set():
            try:
                self.sock.sendall(b"\xFE")
            except OSError:
                return
            time.sleep(0.3)

    def _drain(self):
        try:
            while not self.stop.is_set():
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.log("[qu] the desk closed the connection")
                    return
                self._rx = (self._rx + chunk)[-8192:]
                i = self._rx.find(AH_HDR + bytes([0x7F, 0x11]))
                if i < 0:
                    i = self._rx.find(AH_HDR)
                    while i >= 0 and not (len(self._rx) > i + 10 and self._rx[i + 9] == 0x11):
                        i = self._rx.find(AH_HDR, i + 1)
                if i >= 0 and len(self._rx) > i + 10:
                    end = self._rx.find(b"\xF7", i)
                    if end > 0:
                        rest = self._rx[i + 10:end]
                        dev = self._rx[i + 8]
                        if dev <= 0x0F:  # the reply carries the desk's own MIDI channel
                            self.desk_midi_channel = dev + 1
                            self.status = 0xB0 | dev
                        name = QU_BOX_IDS.get(rest[0], f"box {rest[0]}") if rest else "Qu"
                        self.box = f"{name} firmware {'.'.join(str(x) for x in rest[1:])}"
                        self._rx = self._rx[end + 1:]
                        self._state_reply.set()
        except OSError as e:
            if not self.stop.is_set():
                self.log(f"[qu] connection error: {e}")

    def nrpn(self, pid, va, vx=QU_PEQ_VX) -> bytes:
        s = self.status
        return bytes([s, 0x63, self.ch, s, 0x62, pid, s, 0x06, va & 0x7F, s, 0x26, vx & 0x7F])

    def _send(self, msgs, label):
        data = b"".join(msgs)
        if not self.apply:
            self.log(f"[qu dry-run] {self.target} {label}: {data.hex(' ').upper()}")
            return
        self.sock.sendall(data)
        self.log(f"[qu sent] {self.target} {label}: {data.hex(' ').upper()}")

    def set_notch(self, n: Notch):
        g, f, w, typ = QU_BANDS[n.band]
        msgs = []
        if typ is not None:
            msgs.append(self.nrpn(typ, 0x00))  # bell
        msgs += [self.nrpn(f, self.scale.freq(n.freq_hz)), self.nrpn(w, self.scale.width(n.width_oct)),
                 self.nrpn(g, self.scale.gain(n.gain_db)), self.nrpn(QU_PEQ_IN, 0x01, 0x00)]
        self._send(msgs, f"PEQ {QU_BAND_NAMES[n.band]} {n.freq_hz:.1f} Hz {n.gain_db:+.1f} dB")

    def clear_notch(self, band):
        g, _, _, _ = QU_BANDS[band]
        self._send([self.nrpn(g, self.scale.gain(0.0))], f"PEQ {QU_BAND_NAMES[band]} flat")

    def close(self):
        self.stop.set()
        if self.sock:
            self.sock.close()


class QuGeqDesk(QuDesk):
    """Cuts on the Qu's 28-band graphic EQ (mixes and LR only). Notch.band is the GEQ
    fader number 1..28 (31.5 Hz .. 16 kHz)."""
    name = "qu-geq"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        if not (0x60 <= self.ch <= 0x67):
            raise ValueError("the Qu GEQ is on mixes and LR only: use a target such as Mix1 or LR")

    def set_notch(self, n: Notch):
        msgs = [self.nrpn(QU_GEQ_GAIN, self.scale.gain(n.gain_db), n.band - 1), self.nrpn(QU_GEQ_IN, 0x01, 0x00)]
        self._send(msgs, f"GEQ {QU_GEQ_FREQS[n.band - 1]:g} Hz {n.gain_db:+.1f} dB")

    def clear_notch(self, band):
        self._send([self.nrpn(QU_GEQ_GAIN, self.scale.gain(0.0), band - 1)], f"GEQ {QU_GEQ_FREQS[band - 1]:g} Hz flat")


# ---------------------------------------------------------------------------
# Yamaha DM7 (RCP, TCP 49280)
# ---------------------------------------------------------------------------
class Dm7Desk:
    name = "dm7"
    PORT = 49280

    def __init__(self, host, target, apply=False, log=print):
        self.log = log
        self.box = None
        kind, _, idx = target.partition(":")
        if kind not in ("InCh", "Mix", "Mtrx") or not idx.isdigit():
            raise ValueError("DM7 target looks like InCh:0, Mix:3 or Mtrx:1 (0-based index)")
        self.kind, self.x, self.target = kind, int(idx), target
        self.apply = apply
        self.sock = None
        if apply:
            self.sock = socket.create_connection((host, self.PORT), timeout=5)
            self.sock.settimeout(None)
            self._ok = threading.Event()
            for line in ('scpmode encoding "utf8"', "scpmode keepalive 10000", 'scpmode valuetype "raw"'):
                self._line(line)
            self.stop = threading.Event()
            threading.Thread(target=self._keepalive, daemon=True).start()
            threading.Thread(target=self._drain, daemon=True).start()

    def _keepalive(self):
        while not self.stop.is_set():
            time.sleep(5)
            try:
                self.sock.sendall(b"devstatus runmode\n")
            except OSError:
                return

    def _drain(self):
        buf = b""
        try:
            while True:
                chunk = self.sock.recv(4096)
                if not chunk:
                    return
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.startswith(b"ERROR"):
                        self.log(f"[dm7] {line.decode(errors='replace')}")
                    elif b"productname" in line and line.startswith(b"OK"):
                        self.box = line.decode(errors="replace").split("productname", 1)[1].strip().strip('"')
                        self._ok.set()
        except OSError:
            pass

    @staticmethod
    def check_bands(bands):
        bad = [b for b in bands if not 1 <= b <= 4]
        if bad:
            raise ValueError(f"DM7 PEQ bands are 1-4; got {bad}")

    def check_link(self, timeout=3.0):
        if not self.apply:
            return "dry run (not connected)"
        self._ok.clear()
        self.sock.sendall(b"devinfo productname\n")
        if not self._ok.wait(timeout):
            raise ConnectionError("the DM7 did not answer on TCP 49280. Check the IP (SETUP > NETWORK > For Mixer Control)")
        return self.box

    def _line(self, line):
        if not self.apply:
            self.log(f"[dm7 dry-run] {line}")
            return
        self.sock.sendall((line + "\n").encode())
        self.log(f"[dm7 sent] {line}")

    def _set(self, param, band, value):
        # band is 1-4 as on the desk; RCP's Y index is 0-based
        self._line(f"set MIXER:Current/{self.kind}/PEQ/{param} {self.x} {band - 1} {value}")

    def set_notch(self, n: Notch):
        self._line(f"set MIXER:Current/{self.kind}/PEQ/On {self.x} 0 1")
        self._set("Band/Bypass", n.band, 0)
        self._set("Band/Freq", n.band, round(min(20000, max(20, n.freq_hz)) * 10))
        self._set("Band/Q", n.band, round(min(16.0, max(0.1, octaves_to_q(n.width_oct))) * 1000))
        self._set("Band/Gain", n.band, round(max(-18, min(18, n.gain_db)) * 100))

    def clear_notch(self, band):
        self._set("Band/Gain", band, 0)

    def close(self):
        if self.sock:
            self.stop.set()
            self.sock.close()


def load_calibration(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)
