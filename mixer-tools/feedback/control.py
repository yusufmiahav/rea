"""Manual desk control: fader, mute, pan, preamp, HPF, PEQ and mix sends for one channel.

Qu values follow the Qu MIDI Protocol V1.9+ Iss.2 (NRPN  BN 63 CH  BN 62 ID  BN 06 VA  BN 26 VX):
  fader 17/07, pan 16 (VX 07 = to LR; 00 left, 25 centre, 4A right), mute = note on CH vel 7F/3F,
  local preamp gain 19/07 (-5..+60 dB, table), 48V 69/07, HPF 13/07 + 14/00, PEQ 01..10/07 + 11/00,
  send level 20 / pre-post 50 / assign 55 with VX 00-06 = Mix1..9-10, LR assign 18/07.
DM7 (RCP) addresses follow the ones used elsewhere in this app; pan and send paths are assumed
and should be checked with the DM7 RCP probe.
"""
from __future__ import annotations

import math

from .desks import (QU_BANDS, QU_FADER, QU_HPF_FREQ, QU_HPF_IN, QU_PEQ_IN, QuDesk, QuScale, qu_channel_code,
                    qu_fader_value)
from .qu_routing import ID_ASSIGN, ID_LR, ID_PREPOST, ID_SEND, LR_VX, MIX_VX

QU_PAN, QU_GAIN, QU_48V = 0x16, 0x19, 0x69
# Local preamp gain, dB -> VA (Qu MIDI Protocol reference table)
QU_GAIN_POINTS = [(0x00, -5.0), (0x0A, 0.0), (0x13, 5.0), (0x1D, 10.0), (0x30, 20.0), (0x44, 30.0),
                  (0x57, 40.0), (0x6B, 50.0), (0x7F, 60.0)]
QU_MIXES = list(MIX_VX)                     # Mix1..Mix4, Mix5-6, Mix7-8, Mix9-10


def _interp(points, db):
    for (v0, d0), (v1, d1) in zip(points, points[1:]):
        if d0 <= db <= d1:
            return round(v0 + (v1 - v0) * (db - d0) / (d1 - d0))
    return points[0][0] if db < points[0][1] else points[-1][0]


def _uninterp(points, va):
    for (v0, d0), (v1, d1) in zip(points, points[1:]):
        if v0 <= va <= v1:
            return d0 + (d1 - d0) * (va - v0) / (v1 - v0) if v1 != v0 else d0
    return points[-1][1]


def qu_gain_value(db):
    return _interp(QU_GAIN_POINTS, max(-5.0, min(60.0, db)))


def qu_pan_value(pos):
    """pos -1 (left) .. 0 (centre) .. +1 (right) -> 00..25..4A"""
    pos = max(-1.0, min(1.0, pos))
    return round(0x25 + pos * 0x25)


def qu_fader_db(va):
    from .desks import QU_FADER_POINTS
    return _uninterp(QU_FADER_POINTS, va)


def qu_targets(n_inputs=16):
    return [f"Ip{i}" for i in range(1, n_inputs + 1)] + ["ST1", "ST2", "ST3"] + \
        ["Mix1", "Mix2", "Mix3", "Mix4", "Mix5-6", "Mix7-8", "Mix9-10", "LR"]


QU_EXTRA_CODES = {**{f"FXRtn{i}": 0x07 + i for i in range(1, 5)},     # FX returns 08-0B
                  **{f"FX{i}": i - 1 for i in range(1, 5)},           # FX sends 00-03
                  **{f"DCA{i}": 0x0F + i for i in range(1, 5)},       # DCA 10-13
                  "Grp1-2": 0x68, "Grp3-4": 0x69, "Grp5-6": 0x6A, "Grp7-8": 0x6B,
                  "Mtx1-2": 0x6C, "Mtx3-4": 0x6D}


def qu_code(target):
    from .qu_routing import MIX_CODES
    if target in QU_EXTRA_CODES:
        return QU_EXTRA_CODES[target]
    return MIX_CODES.get(target) or qu_channel_code(target)


class QuControl:
    """Sends manual changes over a QuDesk connection (or logs them in a dry run)."""
    name = "qu"

    def __init__(self, desk: QuDesk, n_inputs=16):
        self.desk = desk
        self.scale = getattr(desk, "scale", None) or QuScale()
        self.n_inputs = n_inputs

    def targets(self):
        return qu_targets(self.n_inputs)

    def mixes(self):
        return list(QU_MIXES)

    @staticmethod
    def is_input(t):
        return t.startswith("Ip") or t.startswith("ST")

    @staticmethod
    def is_mono_input(t):
        return t.startswith("Ip")

    @staticmethod
    def is_mix(t):
        return t.startswith("Mix")

    def _v(self, t):
        v = self.desk.view("Ip1")
        v.ch, v.target = qu_code(t), t
        return v

    def _nrpn(self, t, pid, va, vx=0x07, label=""):
        v = self._v(t)
        v._send([v.nrpn(pid, va, vx)], label)

    def fader(self, t, db):
        self._nrpn(t, QU_FADER, qu_fader_value(db), 0x07, f"fader {'-inf' if db <= -90 else f'{db:+.1f} dB'}")

    def mute(self, t, on):
        v = self._v(t)
        n = 0x90 | (getattr(v, "parent", v).status & 0x0F)
        v._send([bytes([n, v.ch, 0x7F if on else 0x3F, n, v.ch, 0x00])], "mute on" if on else "mute off")

    def pan(self, t, pos):
        self._nrpn(t, QU_PAN, qu_pan_value(pos), LR_VX, f"pan {pos:+.2f}")

    def gain(self, t, db):
        self._nrpn(t, QU_GAIN, qu_gain_value(db), 0x07, f"preamp gain {db:+.0f} dB")

    def phantom(self, t, on):
        self._nrpn(t, QU_48V, 1 if on else 0, 0x07, f"48V {'on' if on else 'off'}")

    def hpf(self, t, on, hz):
        v = self._v(t)
        v._send([v.nrpn(QU_HPF_FREQ, self.scale.hpf(hz), 0x07), v.nrpn(QU_HPF_IN, 1 if on else 0, 0x00)],
                f"HPF {'in' if on else 'out'} {hz:.0f} Hz")

    def peq_in(self, t, on):
        self._nrpn(t, QU_PEQ_IN, 1 if on else 0, 0x00, f"PEQ {'in' if on else 'out'}")

    def peq_band(self, t, band, gain_db, hz, width_oct):
        gid, fid, wid, _ = QU_BANDS[band]
        v = self._v(t)
        v._send([v.nrpn(gid, self.scale.gain(gain_db)), v.nrpn(fid, self.scale.freq(hz)),
                 v.nrpn(wid, self.scale.width(width_oct))],
                f"PEQ band {band}: {gain_db:+.1f} dB {hz:.0f} Hz {width_oct:.2f} oct")

    def has_geq(self, t):
        return self.is_mix(t) or t == "LR"

    def geq_flat(self, t):
        """All 28 GEQ faders to 0 dB (mixes and LR)."""
        from .desks import QU_GEQ_GAIN
        v = self._v(t)
        v._send([v.nrpn(QU_GEQ_GAIN, self.scale.gain(0.0), i) for i in range(28)], "GEQ flat (all 28 faders 0 dB)")

    def send(self, t, mix, on, db, post=True):
        vx = MIX_VX[mix]
        v = self._v(t)
        v._send([v.nrpn(ID_ASSIGN, 1 if on else 0, vx), v.nrpn(ID_PREPOST, 0 if post else 1, vx),
                 v.nrpn(ID_SEND, qu_fader_value(db), vx)],
                f"-> {mix} {'on' if on else 'off'} {'post' if post else 'pre'} {db:+.1f} dB")

    def lr_assign(self, t, on):
        self._nrpn(t, ID_LR, 1 if on else 0, LR_VX, f"L+R {'on' if on else 'off'}")

    def read(self, t):
        """What the desk has reported for this channel (after Read from desk), in real units."""
        p = getattr(self.desk, "params", None)
        if p is None:
            return {}
        ch = qu_code(t)
        out = {}
        va = p.get(ch, QU_FADER, 0x07)
        if va is not None:
            out["fader"] = qu_fader_db(va)
        va = p.get(ch, QU_PAN, LR_VX)
        if va is not None:
            out["pan"] = (va - 0x25) / 0x25
        va = p.get(ch, QU_GAIN, 0x07)
        if va is not None:
            out["gain"] = _uninterp(QU_GAIN_POINTS, va)
        for key, pid, vx in (("phantom", QU_48V, 0x07), ("peq_in", QU_PEQ_IN, 0x00), ("hpf_in", QU_HPF_IN, 0x00),
                             ("lr", ID_LR, LR_VX)):
            va = p.get(ch, pid, vx)
            if va is not None:
                out[key] = bool(va)
        va = p.get(ch, QU_HPF_FREQ, 0x07)
        if va is not None:
            out["hpf_hz"] = 20.0 * 1000.0 ** (va / 127)
        for b, (gid, fid, wid, _) in QU_BANDS.items():
            g, f, w = p.get(ch, gid, 0x07), p.get(ch, fid, 0x07), p.get(ch, wid, 0x07)
            if None not in (g, f, w):
                out[f"band{b}"] = ((g - 64) * 0.1875, 20.0 * 1000.0 ** (f / 127), 1.5 / (13.5 ** (w / 127)))
        for m, vx in MIX_VX.items():
            on, lv, pre = p.get(ch, ID_ASSIGN, vx), p.get(ch, ID_SEND, vx), p.get(ch, ID_PREPOST, vx)
            if on is not None or lv is not None:
                out[f"send:{m}"] = (bool(on), qu_fader_db(lv) if lv is not None else -90.0, not pre)
        mutes = getattr(p, "mutes", {})
        if ch in mutes:
            out["mute"] = mutes[ch]
        return out


class Dm7Control:
    """DM7 over RCP. Fader, HPF and PEQ addresses as used by the suppressor; mute uses
    Fader/On (0 = muted), pan uses ToSt/Pan (-63..63) and sends ToMix/On + ToMix/Level (dB x 100),
    which are assumed from Yamaha's RCP naming and should be checked on the desk."""
    name = "dm7"

    def __init__(self, desk, n_inputs=48, n_mixes=24):
        self.desk, self.n_inputs, self.n_mixes = desk, n_inputs, n_mixes

    def targets(self):
        return [f"InCh:{i}" for i in range(self.n_inputs)] + [f"Mix:{i}" for i in range(self.n_mixes)]

    @staticmethod
    def is_input(t):
        return t.startswith("InCh")

    is_mono_input = is_input

    @staticmethod
    def is_mix(t):
        return t.startswith("Mix")

    def _line(self, t, path, y, value):
        v = self.desk.view(t)
        v._line(f"set MIXER:Current/{v.kind}/{path} {v.x} {y} {value}")

    def fader(self, t, db):
        self.desk.view(t).set_fader(db)

    def mute(self, t, on):
        self._line(t, "Fader/On", 0, 0 if on else 1)

    def pan(self, t, pos):
        self._line(t, "ToSt/Pan", 0, round(max(-1.0, min(1.0, pos)) * 63))

    def gain(self, t, db):
        self.desk.log("[dm7] preamp gain isn't on the DM7's channel address; set it on the desk (I/O)")

    def phantom(self, t, on):
        self.desk.log("[dm7] 48V isn't on the DM7's channel address; set it on the desk (I/O)")

    def hpf(self, t, on, hz):
        self._line(t, "HPF/On", 0, 1 if on else 0)
        self._line(t, "HPF/Freq", 0, round(min(2000, max(20, hz)) * 10))

    def peq_in(self, t, on):
        self._line(t, "PEQ/On", 0, 1 if on else 0)

    def peq_band(self, t, band, gain_db, hz, width_oct):
        from .notches import octaves_to_q
        self._line(t, "PEQ/Band/Freq", band - 1, round(min(20000, max(20, hz)) * 10))
        self._line(t, "PEQ/Band/Q", band - 1, round(min(16.0, max(0.1, octaves_to_q(width_oct))) * 1000))
        self._line(t, "PEQ/Band/Gain", band - 1, round(max(-18, min(18, gain_db)) * 100))

    def mixes(self):
        return [f"Mix:{i}" for i in range(self.n_mixes)]

    def has_geq(self, t):
        return False

    def geq_flat(self, t):
        self.desk.log("[dm7] the DM7's GEQ is in the rack, not on the channel; flatten it on the desk")

    def send(self, t, mix, on, db, post=True):
        y = int(mix.split(":")[1])
        self._line(t, "ToMix/On", y, 1 if on else 0)
        self._line(t, "ToMix/Level", y, -32768 if db <= -90 else round(max(-90.0, min(10.0, db)) * 100))

    def lr_assign(self, t, on):
        self._line(t, "ToSt/On", 0, 1 if on else 0)

    def read(self, t):
        return {}


def width_from_slider(x):
    """0..1 slider -> 1.5 .. 1/9 octave (log)."""
    return 1.5 / (13.5 ** max(0.0, min(1.0, x)))


def freq_from_slider(x):
    """0..1 slider -> 20 Hz .. 20 kHz (log)."""
    return 20.0 * 1000.0 ** max(0.0, min(1.0, x))


def slider_from_freq(hz):
    return math.log(max(20.0, min(20000.0, hz)) / 20.0) / math.log(1000.0)


def slider_from_width(octv):
    return math.log(1.5 / max(1 / 9, min(1.5, octv))) / math.log(13.5)
