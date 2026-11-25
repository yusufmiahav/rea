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

    def __init__(self, target="(none)"):
        self.target = target

    def set_notch(self, n: Notch):
        print(f"[dry-run] {self.target} band {n.band}: {n.freq_hz:7.1f} Hz  {n.gain_db:+.1f} dB  {n.width_oct:.3f} oct")

    def clear_notch(self, band):
        print(f"[dry-run] {self.target} band {band}: flat")

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Allen & Heath Qu-16 / 24 / 32 / Pac / SB
# ---------------------------------------------------------------------------
QU_BANDS = {  # band id -> (gain ID, freq ID, width ID, type ID or None)
    0: (0x01, 0x02, 0x03, 0x04),  # LF (set to bell when used)
    1: (0x05, 0x06, 0x07, None),  # LM
    2: (0x09, 0x0A, 0x0B, None),  # HM
    3: (0x0D, 0x0E, 0x0F, 0x10),  # HF (set to bell when used)
}
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

    def __init__(self, host, target, midi_channel=1, apply=False, calibration=None):
        self.ch = qu_channel_code(target)
        self.target = target
        self.status = 0xB0 | ((midi_channel - 1) & 0x0F)
        self.scale = QuScale(calibration)
        self.apply = apply
        self.sock = None
        self.stop = threading.Event()
        if apply:
            self.sock = socket.create_connection((host, self.PORT), timeout=5)
            threading.Thread(target=self._keepalive, daemon=True).start()
            threading.Thread(target=self._drain, daemon=True).start()

    def _keepalive(self):
        while not self.stop.is_set():
            try:
                self.sock.sendall(b"\xFE")
            except OSError:
                return
            time.sleep(0.3)

    def _drain(self):
        try:
            while not self.stop.is_set() and self.sock.recv(4096):
                pass
        except OSError:
            pass

    def nrpn(self, pid, va, vx=QU_PEQ_VX) -> bytes:
        s = self.status
        return bytes([s, 0x63, self.ch, s, 0x62, pid, s, 0x06, va & 0x7F, s, 0x26, vx & 0x7F])

    def _send(self, msgs, label):
        data = b"".join(msgs)
        if not self.apply:
            print(f"[qu dry-run] {self.target} {label}: {data.hex(' ').upper()}")
            return
        self.sock.sendall(data)

    def set_notch(self, n: Notch):
        g, f, w, typ = QU_BANDS[n.band]
        msgs = []
        if typ is not None:
            msgs.append(self.nrpn(typ, 0x00))  # bell
        msgs += [self.nrpn(f, self.scale.freq(n.freq_hz)), self.nrpn(w, self.scale.width(n.width_oct)),
                 self.nrpn(g, self.scale.gain(n.gain_db)), self.nrpn(QU_PEQ_IN, 0x01, 0x00)]
        self._send(msgs, f"band {n.band} {n.freq_hz:.1f} Hz {n.gain_db:+.1f} dB")

    def clear_notch(self, band):
        g, _, _, _ = QU_BANDS[band]
        self._send([self.nrpn(g, self.scale.gain(0.0))], f"band {band} flat")

    def close(self):
        self.stop.set()
        if self.sock:
            self.sock.close()


# ---------------------------------------------------------------------------
# Yamaha DM7 (RCP, TCP 49280)
# ---------------------------------------------------------------------------
class Dm7Desk:
    name = "dm7"
    PORT = 49280

    def __init__(self, host, target, apply=False):
        kind, _, idx = target.partition(":")
        if kind not in ("InCh", "Mix", "Mtrx") or not idx.isdigit():
            raise ValueError("DM7 target looks like InCh:0, Mix:3 or Mtrx:1 (0-based index)")
        self.kind, self.x, self.target = kind, int(idx), target
        self.apply = apply
        self.sock = None
        if apply:
            self.sock = socket.create_connection((host, self.PORT), timeout=5)
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
                        print(f"[dm7] {line.decode(errors='replace')}")
        except OSError:
            pass

    def _line(self, line):
        if not self.apply:
            print(f"[dm7 dry-run] {line}")
            return
        self.sock.sendall((line + "\n").encode())

    def _set(self, param, band, value):
        self._line(f"set MIXER:Current/{self.kind}/PEQ/{param} {self.x} {band} {value}")

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
