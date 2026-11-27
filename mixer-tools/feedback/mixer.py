"""Desk models and controllers for the Mixer page (channel strips for every desk).

Every controller has the same calls, so the strip view does not care which desk it talks to:

    layers()                 [(bank name, [target, ...]), ...]   what the bank buttons show
    buses()                  [(bus, label), ...]                  mixes/aux/FX the faders can flip to
    main_target()            the master strip (LR / Stereo)
    label(t), colour(t)      strip name (desk name when the desk reports one) and colour
    state(t)                 {"fader": dB, "mute": bool, "pan": -1..1}  (what the desk reported)
    send_state(t, bus)       (on, dB) or None
    fader/mute/pan(t, ...)   change the desk
    send_level/send_on(t, bus, ...)
    refresh(targets)         ask the desk for the values of these strips (names, faders, mutes)

The protocols come from the public vendor documents (see ../public-vendor-protocols.md):
  * Qu-16/24/32/Pac/SB: Qu MIDI Protocol V1.9 (NRPN channel + parameter ID), TCP 51325
  * SQ-5/6/7 and Qu-5/6/7: SQ MIDI Protocol Issue 5 / Qu-5/6/7 MIDI Protocol Issue 2 (14-bit NRPN
    parameter address with coarse/fine value), TCP 51325
  * dLive and Avantis: MIDI over TCP protocol (channel-type offsets, NRPN 17 faders, SysEx names,
    colours and sends), TCP 51325
  * Yamaha DM7 / DM3 / QL / CL: text remote control protocol (RCP), TCP 49280, MIXER:Current/...
Addresses marked ASSUMED were derived from the documented layout but are not printed in the docs.
"""
from __future__ import annotations

import re
import socket
import threading
import time

from .control import QuControl, qu_fader_db
from .desks import AH_HDR as QU_HDR, QU_FADER, qu_fader_value
from .qu_routing import ID_ASSIGN, ID_LR, ID_PREPOST, ID_SEND, LR_VX, MIX_VX

# ---------------------------------------------------------------------------
# Desk models
# ---------------------------------------------------------------------------
# family: qu = Qu MIDI V1.9, sq = SQ-style 14-bit NRPN, ah = dLive/Avantis MIDI over TCP, rcp = Yamaha RCP
MODELS = {
    "Allen & Heath Qu-16": dict(family="qu", inputs=16, groups=False),
    "Allen & Heath Qu-24": dict(family="qu", inputs=24, groups=True),
    "Allen & Heath Qu-32": dict(family="qu", inputs=32, groups=True),
    "Allen & Heath Qu-Pac": dict(family="qu", inputs=32, groups=True),
    "Allen & Heath Qu-SB": dict(family="qu", inputs=32, groups=True),
    "Allen & Heath Qu-5 / Qu-6 / Qu-7": dict(family="sq", qu567=True, inputs=32, groups=12, fxrtn=6, aux=12,
                                              fxsend=4, mtx=3, dca=8),
    "Allen & Heath SQ-5 / SQ-6 / SQ-7": dict(family="sq", qu567=False, inputs=48, groups=12, fxrtn=8, aux=12,
                                              fxsend=4, mtx=3, dca=8),
    "Allen & Heath dLive": dict(family="ah", desk="dlive"),
    "Allen & Heath Avantis": dict(family="ah", desk="avantis"),
    "Yamaha DM7": dict(family="rcp", counts={"InCh": 120, "Mix": 48, "Mtrx": 12, "St": 2, "DCA": 24}),
    "Yamaha DM7 Compact": dict(family="rcp", counts={"InCh": 72, "Mix": 48, "Mtrx": 12, "St": 2, "DCA": 24}),
    "Yamaha DM3": dict(family="rcp", counts={"InCh": 16, "StInCh": 1, "Mix": 6, "Mtrx": 2, "St": 1, "DCA": 8}),
    "Yamaha QL5": dict(family="rcp", counts={"InCh": 64, "StInCh": 8, "Mix": 16, "Mtrx": 8, "St": 1, "Mono": 1, "DCA": 16}),
    "Yamaha QL1": dict(family="rcp", counts={"InCh": 32, "StInCh": 8, "Mix": 16, "Mtrx": 8, "St": 1, "Mono": 1, "DCA": 16}),
    "Yamaha CL5": dict(family="rcp", counts={"InCh": 72, "StInCh": 8, "Mix": 24, "Mtrx": 8, "St": 1, "Mono": 1, "DCA": 16}),
    "Yamaha CL3": dict(family="rcp", counts={"InCh": 64, "StInCh": 8, "Mix": 24, "Mtrx": 8, "St": 1, "Mono": 1, "DCA": 16}),
    "Yamaha CL1": dict(family="rcp", counts={"InCh": 48, "StInCh": 8, "Mix": 24, "Mtrx": 8, "St": 1, "Mono": 1, "DCA": 16}),
}
PORT = {"qu": 51325, "sq": 51325, "ah": 51325, "rcp": 49280}


def banks(names, size=16, prefix="Ch"):
    out = []
    for i in range(0, len(names), size):
        chunk = names[i:i + size]
        out.append((f"{prefix} {i + 1}-{i + len(chunk)}", chunk))
    return out


class Unsupported:
    """Defaults for the channel editor calls a desk family doesn't have."""
    refresh_on_bank = True    # ask the desk for the strips each time the bank changes

    def _no(self, what):
        self.log(f"[{self.name}] {what} isn't available on {self.model} from this app")

    def gain(self, t, db): self._no("preamp gain")
    def phantom(self, t, on): self._no("48V")
    def hpf(self, t, on, hz): self._no("the HPF")
    def peq_in(self, t, on): self._no("the PEQ")
    def peq_band(self, t, band, gain_db, hz, width_oct): self._no("the PEQ")
    def geq_flat(self, t): self._no("the GEQ")
    def has_geq(self, t): return False
    def lr_assign(self, t, on): self._no("L+R assign")
    def group_assign(self, t, grp, on): self._no("group assign")
    def groups(self): return []
    def colour(self, t): return None
    def caps(self, t): return {"fader", "mute"}
    def short(self, t): return t
    def read(self, t):
        st = self.state(t)
        out = {k: st[k] for k in ("fader", "mute", "pan") if k in st}
        for bus, _ in self.buses():
            s = self.send_state(t, bus)
            if s is not None:
                out[f"send:{bus}"] = (s[0], s[1], True)
        return out
    def mixes(self): return [b for b, _ in self.buses()]
    def send(self, t, mix, on, db, post=True):
        self.send_on(t, mix, on)
        self.send_level(t, mix, db)


# ---------------------------------------------------------------------------
# Qu-16 / 24 / 32 / Pac / SB  (extends the existing QuControl)
# ---------------------------------------------------------------------------
# Send index (VX) for ID 20 send level / 50 pre-post / 55 assign (Qu MIDI Protocol V1.9)
QU_BUS_VX = {**MIX_VX, **{f"FX{i}": 0x0F + i for i in range(1, 5)},
             "Grp1-2": 0x08, "Grp3-4": 0x09, "Grp5-6": 0x0A, "Grp7-8": 0x0B, "Mtx1-2": 0x0C, "Mtx3-4": 0x0D}
QU_PAN = 0x16
QU_GROUP_MODE = 0x5E


class QuMixer(QuControl, Unsupported):
    name = "qu"

    def __init__(self, desk, model="Allen & Heath Qu-16"):
        spec = MODELS[model]
        super().__init__(desk, n_inputs=spec["inputs"])
        self.model, self.has_groups = model, spec["groups"]

    @property
    def log(self):
        return self.desk.log

    def targets(self):
        t = [f"Ip{i}" for i in range(1, self.n_inputs + 1)] + ["ST1", "ST2", "ST3"]
        t += [f"FXRtn{i}" for i in range(1, 5)]
        t += ["Mix1", "Mix2", "Mix3", "Mix4", "Mix5-6", "Mix7-8", "Mix9-10", "LR"]
        if self.has_groups:
            t += ["Grp1-2", "Grp3-4", "Grp5-6", "Grp7-8", "Mtx1-2", "Mtx3-4"]
        t += [f"FX{i}" for i in range(1, 5)] + [f"DCA{i}" for i in range(1, 5)]
        return t

    def layers(self):
        ins = [f"Ip{i}" for i in range(1, self.n_inputs + 1)]
        out = banks(ins, 16, "Ch")
        out.append(("ST / FX ret", ["ST1", "ST2", "ST3", "FXRtn1", "FXRtn2", "FXRtn3", "FXRtn4"]))
        out.append(("Mixes", ["Mix1", "Mix2", "Mix3", "Mix4", "Mix5-6", "Mix7-8", "Mix9-10"]))
        if self.has_groups:
            out.append(("Groups / Mtx", ["Grp1-2", "Grp3-4", "Grp5-6", "Grp7-8", "Mtx1-2", "Mtx3-4"]))
        out.append(("FX / DCA", [f"FX{i}" for i in range(1, 5)] + [f"DCA{i}" for i in range(1, 5)]))
        return out

    def main_target(self):
        return "LR"

    def buses(self):
        b = [(m, m) for m in ("Mix1", "Mix2", "Mix3", "Mix4", "Mix5-6", "Mix7-8", "Mix9-10")]
        b += [(f"FX{i}", f"FX{i}") for i in range(1, 5)]
        return b

    def groups(self):
        return ["Grp1-2", "Grp3-4", "Grp5-6", "Grp7-8"] if self.has_groups else []

    def is_input(self, t):
        return t.startswith(("Ip", "ST", "FXRtn"))

    @staticmethod
    def is_mono_input(t):
        return t.startswith("Ip")

    @staticmethod
    def is_mix(t):
        return t.startswith(("Mix", "Grp", "Mtx", "FX")) and not t.startswith("FXRtn")

    def caps(self, t):
        c = {"fader", "mute"}
        if self.is_input(t) or t.startswith(("Mix", "Grp", "Mtx")) or t == "LR":
            c |= {"pan", "eq"}
        if self.is_input(t):
            c.add("sends")
        if t.startswith("Ip"):
            c |= {"preamp", "hpf"}
        if t.startswith("ST"):
            c.add("hpf")
        return c

    def mixes(self):
        return [b for b, _ in self.buses()]

    def has_geq(self, t):
        return t.startswith("Mix") or t == "LR"

    def _params(self):
        return getattr(getattr(self.desk, "parent", self.desk), "params", None)

    def label(self, t):
        p = self._params()
        name = p.names.get(QuMixer.code(t)) if p is not None and hasattr(p, "names") else None
        if name:
            return name
        if t.startswith("Mix") and self.group_mode(t):
            return t.replace("Mix", "Grp ")
        return t

    @staticmethod
    def code(t):
        from .control import qu_code
        return qu_code(t)

    def _nrpn(self, t, pid, va, vx=0x07, label=""):
        QuControl._nrpn(self, t, pid, va, vx, label)
        p = self._params()
        if p is not None:
            p.params[(self.code(t), pid, vx)] = va

    def mute(self, t, on):
        QuControl.mute(self, t, on)
        p = self._params()
        if p is not None:
            p.mutes[self.code(t)] = bool(on)

    refresh_on_bank = False   # one Get System State reports the whole desk

    def group_mode(self, t):
        """True when a stereo mix is set to group mode on the desk (NRPN 5E, VA 00 = group)."""
        p = self._params()
        if p is None:
            return False
        va = p.get(self.code(t), QU_GROUP_MODE)
        return va == 0

    def state(self, t):
        p = self._params()
        if p is None:
            return {}
        ch = self.code(t)
        out = {}
        va = p.get(ch, QU_FADER, 0x07)
        if va is not None:
            out["fader"] = qu_fader_db(va)
        va = p.get(ch, QU_PAN, LR_VX)
        if va is not None:
            out["pan"] = (va - 0x25) / 0x25
        if ch in p.mutes:
            out["mute"] = p.mutes[ch]
        return out

    def send_state(self, t, bus):
        p = self._params()
        if p is None:
            return None
        ch, vx = self.code(t), QU_BUS_VX[bus]
        on, lv = p.get(ch, ID_ASSIGN, vx), p.get(ch, ID_SEND, vx)
        if on is None and lv is None:
            return None
        return bool(on), (qu_fader_db(lv) if lv is not None else -90.0)

    def send_level(self, t, bus, db):
        self._nrpn(t, ID_SEND, qu_fader_value(db), QU_BUS_VX[bus], f"send to {bus} {db:+.1f} dB")

    def send_on(self, t, bus, on):
        self._nrpn(t, ID_ASSIGN, 1 if on else 0, QU_BUS_VX[bus], f"{bus} assign {'on' if on else 'off'}")

    def send(self, t, mix, on, db, post=True):
        vx = QU_BUS_VX[mix]
        v = self._v(t)
        msgs = [v.nrpn(ID_ASSIGN, 1 if on else 0, vx)]
        if not mix.startswith("Grp") and not self.group_mode(mix):
            msgs += [v.nrpn(ID_PREPOST, 0 if post else 1, vx), v.nrpn(ID_SEND, qu_fader_value(db), vx)]
        v._send(msgs, f"-> {mix} {'on' if on else 'off'} {'post' if post else 'pre'} {db:+.1f} dB")

    def group_assign(self, t, grp, on):
        self._nrpn(t, ID_ASSIGN, 1 if on else 0, QU_BUS_VX[grp], f"{grp} assign {'on' if on else 'off'}")

    def read(self, t):
        out = QuControl.read(self, t)
        p = self._params()
        if p is not None:
            for g in self.groups():
                va = p.get(self.code(t), ID_ASSIGN, QU_BUS_VX[g])
                if va is not None:
                    out[f"group:{g}"] = bool(va)
            for b in ("FX1", "FX2", "FX3", "FX4"):
                s = self.send_state(t, b)
                if s is not None:
                    out[f"send:{b}"] = (s[0], s[1], True)
        return out

    def refresh(self, targets=None, bus=None):
        """Get System State (the desk then reports every parameter), then ask for channel names."""
        desk = getattr(self.desk, "parent", self.desk)
        if not getattr(desk, "apply", False):
            self.log("[qu dry-run] would ask the desk for its state and channel names")
            return
        desk.check_link()
        dev = desk.status & 0x0F
        codes = sorted({self.code(t) for t in (targets or self.targets())})
        for c in codes:
            desk.sock.sendall(QU_HDR + bytes([dev, 0x01, c, 0xF7]))
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# Generic MIDI-over-TCP client (SQ, Qu-5/6/7, dLive, Avantis)
# ---------------------------------------------------------------------------
class MidiTcp:
    """One TCP connection carrying raw MIDI. Parses what the desk sends:
    nrpn[(midi_ch, msb, lsb)] = [coarse, fine or None], notes[(midi_ch, note)] = velocity, sysex list."""

    def __init__(self, host, apply=False, log=print, tag="midi", port=51325, active_sense=False):
        self.apply, self.log, self.tag = apply, log, tag
        self.sock = None
        self.nrpn, self.notes = {}, {}
        self.on_sysex = None
        self.stop = threading.Event()
        self._status, self._data, self._sx, self._cur = None, [], None, {}
        self.lock = threading.Lock()
        if apply:
            self.sock = socket.create_connection((host, port), timeout=5)
            self.sock.settimeout(None)
            threading.Thread(target=self._reader, daemon=True).start()
            if active_sense:
                threading.Thread(target=self._sense, daemon=True).start()

    def send(self, data: bytes, label=""):
        if not self.apply:
            self.log(f"[{self.tag} dry-run] {label}: {data.hex(' ').upper()}")
            return
        with self.lock:
            self.sock.sendall(data)
        if label:
            self.log(f"[{self.tag} sent] {label}: {data.hex(' ').upper()}")

    def send_quiet(self, data: bytes):
        if self.apply:
            with self.lock:
                self.sock.sendall(data)

    def _sense(self):
        while not self.stop.is_set():
            try:
                self.send_quiet(b"\xFE")
            except OSError:
                return
            time.sleep(0.3)

    def _reader(self):
        try:
            while not self.stop.is_set():
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.log(f"[{self.tag}] the desk closed the connection")
                    return
                self.feed(chunk)
        except OSError as e:
            if not self.stop.is_set():
                self.log(f"[{self.tag}] connection error: {e}")

    def feed(self, data: bytes):
        for b in data:
            if self._sx is not None:
                if b == 0xF7:
                    self._sx.append(b)
                    sx, self._sx = bytes(self._sx), None
                    if self.on_sysex:
                        try:
                            self.on_sysex(sx)
                        except Exception as e:  # a bad message must not kill the reader
                            self.log(f"[{self.tag}] sysex parse: {e}")
                elif b < 0x80:
                    self._sx.append(b)
                elif b < 0xF8:
                    self._sx = None
                    self._status, self._data = b, []
                continue
            if b == 0xF0:
                self._sx = bytearray([b])
            elif b >= 0xF8:
                continue
            elif b & 0x80:
                self._status, self._data = b, []
            elif self._status is not None:
                self._data.append(b)
                need = 1 if self._status & 0xF0 in (0xC0, 0xD0) else 2
                if len(self._data) == need:
                    self._msg(self._status, self._data)
                    self._data = []

    def _msg(self, st, d):
        kind, ch = st & 0xF0, st & 0x0F
        if kind == 0xB0:
            cur = self._cur.setdefault(ch, {})
            cc, val = d
            if cc == 0x63:
                cur.clear(); cur["msb"] = val
            elif cc == 0x62:
                cur["lsb"] = val
            elif cc == 0x06 and "msb" in cur and "lsb" in cur:
                cur["vc"] = val
                self.nrpn[(ch, cur["msb"], cur["lsb"])] = [val, None]
            elif cc == 0x26 and "vc" in cur:
                self.nrpn[(ch, cur["msb"], cur["lsb"])] = [cur["vc"], val]
        elif kind == 0x90 and d[1] > 0:
            self.notes[(ch, d[0])] = d[1]

    def close(self):
        self.stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# SQ-5/6/7 and Qu-5/6/7: 14-bit NRPN parameter addresses
# ---------------------------------------------------------------------------
# Linear-taper level, 14-bit (VC*128+VF): the doc's table is a straight line in dB:
# 0 dB = 76 5C (15196), about 118.75 steps per dB, +10 dB = 7F 7F, -inf = 00 00.
SQ_LIN_0DB, SQ_LIN_PER_DB = 15196, 118.75
# Audio taper (desk setting "Fader Law: Audio Taper"), doc table points (dB, VC, VF)
SQ_AUDIO_POINTS = [(-90.0, 0x00, 0x00), (-45, 0x0C, 0x00), (-30, 0x1F, 0x00), (-20, 0x2E, 0x40), (-10, 0x3E, 0x00),
                   (-5, 0x4E, 0x40), (0, 0x62, 0x00), (5, 0x73, 0x40), (10, 0x7F, 0x40)]


def sq_level_value(db, taper="linear"):
    if db <= -89.5:
        return 0
    db = min(10.0, db)
    if taper == "audio":
        pts = [(d, (c << 7) | f) for d, c, f in SQ_AUDIO_POINTS]
        for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
            if d0 <= db <= d1:
                return round(v0 + (v1 - v0) * (db - d0) / (d1 - d0))
        return pts[0][1]
    return max(0, min(16383, round(SQ_LIN_0DB + db * SQ_LIN_PER_DB)))


def sq_level_db(v, taper="linear"):
    if v <= 0:
        return -90.0
    if taper == "audio":
        pts = [(d, (c << 7) | f) for d, c, f in SQ_AUDIO_POINTS]
        for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
            if v0 <= v <= v1:
                return d0 + (d1 - d0) * (v - v0) / (v1 - v0)
        return 10.0
    return max(-90.0, (v - SQ_LIN_0DB) / SQ_LIN_PER_DB)


class SqAddress:
    """Parameter address map from the SQ MIDI Protocol Issue 5 (also used by Qu-5/6/7 Issue 2).
    Sources: Ip1-48 = 0-47, Grp1-12 = 48-59, FXRtn1-8 = 60-67 (Qu-5/6/7: ST1 = 32, ST2 = 34, USB = 36)."""
    LEVEL_LR = 0x2000          # 40 00 + source          (doc: Ip1 LR 40 00, Grp1 LR 40 30, FX1Rtn LR 40 3C)
    LEVEL_AUX = 0x2044         # 40 44 + src*12 + aux-1   (doc: Ip48 Aux1 44 78, Grp1 Aux1 45 04, FX1Rtn Aux1 46 14)
    LEVEL_FX = 0x2614          # 4C 14 + src*4 + fx-1     (doc: Ip1 FX1-4 4C 14-17; later rows ASSUMED)
    PAN_OFFSET = 0x0800        # pan = level address + 0x800  (50 xx vs 40 xx; doc: Ip24 Aux5 pan 52 5C)
    ASSIGN_OFFSET = 0x1000     # assign = level address + 0x1000 (60 xx vs 40 xx)
    GROUP_ASSIGN = 0x3374      # 66 74 + input*12 + grp-1 (doc: Ip1 Grp1 66 74, Ip2 Grp1 67 00, Ip23 Grp1 68 7C)
    OUT_LEVEL = 0x2780         # 4F 00: LR; +1..12 Aux; +13..16 FX send; +17..19 Mtx
    DCA_LEVEL = 0x27A0         # 4F 20 + dca-1
    MUTE = {"Ip": 0x00, "Grp": 0x30, "FXRtn": 0x3C, "LR": 0x44, "Aux": 0x45, "FX": 0x51, "Mtx": 0x55,
            "DCA": 0x100, "MGrp": 0x200}

    @staticmethod
    def split(a):
        return (a >> 7) & 0x7F, a & 0x7F


class SqMixer(Unsupported):
    name = "sq"

    def __init__(self, client: MidiTcp, model, midi_channel=1, taper="linear"):
        self.c, self.model, self.spec = client, model, MODELS[model]
        self.desk = client
        self.status = 0xB0 | ((midi_channel - 1) & 0x0F)
        self.taper = taper
        self.qu567 = self.spec["qu567"]
        self.mix_word = "Mix" if self.qu567 else "Aux"

    @property
    def log(self):
        return self.c.log

    # --- names ------------------------------------------------------------
    def _inputs(self):
        ins = [f"Ip{i}" for i in range(1, self.spec["inputs"] + 1)]
        if self.qu567:
            ins += ["ST1", "ST2", "USB"]
        return ins

    def targets(self):
        s = self.spec
        return (self._inputs() + [f"FXRtn{i}" for i in range(1, s["fxrtn"] + 1)] +
                [f"Grp{i}" for i in range(1, s["groups"] + 1)] + [f"Aux{i}" for i in range(1, s["aux"] + 1)] +
                [f"FX{i}" for i in range(1, s["fxsend"] + 1)] + [f"Mtx{i}" for i in range(1, s["mtx"] + 1)] +
                [f"DCA{i}" for i in range(1, s["dca"] + 1)] + ["LR"])

    def layers(self):
        s = self.spec
        out = banks([f"Ip{i}" for i in range(1, s["inputs"] + 1)], 16, "Ch")
        extra = (["ST1", "ST2", "USB"] if self.qu567 else []) + [f"FXRtn{i}" for i in range(1, s["fxrtn"] + 1)]
        out.append(("ST / FX ret" if self.qu567 else "FX returns", extra))
        out.append(("Groups", [f"Grp{i}" for i in range(1, s["groups"] + 1)]))
        out.append((f"{self.mix_word}es" if self.qu567 else "Auxes", [f"Aux{i}" for i in range(1, s["aux"] + 1)]))
        out.append(("FX / Mtx", [f"FX{i}" for i in range(1, s["fxsend"] + 1)] + [f"Mtx{i}" for i in range(1, s["mtx"] + 1)]))
        out.append(("DCA", [f"DCA{i}" for i in range(1, s["dca"] + 1)]))
        return out

    def main_target(self):
        return "LR"

    def label(self, t):
        if self.qu567 and t.startswith("Aux"):
            return "Mix" + t[3:]
        return t

    def buses(self):
        return ([(f"Aux{i}", self.label(f"Aux{i}")) for i in range(1, self.spec["aux"] + 1)] +
                [(f"FX{i}", f"FX{i}") for i in range(1, self.spec["fxsend"] + 1)])

    def groups(self):
        return [f"Grp{i}" for i in range(1, self.spec["groups"] + 1)]

    @staticmethod
    def _kind(t):
        m = re.match(r"([A-Za-z]+)(\d*)", t)
        return m.group(1), int(m.group(2) or 0)

    def source(self, t):
        """Source index used by the level/pan/assign tables, or None for an output."""
        k, n = self._kind(t)
        if k == "Ip":
            return n - 1
        if k in ("ST", "USB"):
            return {"ST1": 32, "ST2": 34, "USB": 36}[t]
        if k == "Grp":
            return 48 + n - 1
        if k == "FXRtn":
            return 60 + n - 1
        return None

    def is_input(self, t):
        k, _ = self._kind(t)
        return k in ("Ip", "ST", "USB", "FXRtn")

    def is_mono_input(self, t):
        return t.startswith("Ip")

    def is_mix(self, t):
        return self._kind(t)[0] in ("Aux", "FX", "Mtx", "Grp")

    def caps(self, t):
        c = {"fader", "mute"}
        if self.source(t) is not None:
            c.add("pan")
        if self.is_input(t):
            c.add("sends")
        return c

    # --- addresses --------------------------------------------------------
    def fader_addr(self, t):
        s = self.source(t)
        if s is not None:
            return SqAddress.LEVEL_LR + s
        k, n = self._kind(t)
        if k == "LR":
            return SqAddress.OUT_LEVEL
        if k == "Aux":
            return SqAddress.OUT_LEVEL + n
        if k == "FX":
            return SqAddress.OUT_LEVEL + 12 + n
        if k == "Mtx":
            return SqAddress.OUT_LEVEL + 16 + n
        if k == "DCA":
            return SqAddress.DCA_LEVEL + n - 1
        raise ValueError(t)

    def mute_addr(self, t):
        k, n = self._kind(t)
        if k in ("ST", "USB"):
            return self.source(t)
        if k == "LR":
            return SqAddress.MUTE["LR"]
        return SqAddress.MUTE[k] + n - 1

    def send_addr(self, t, bus):
        s = self.source(t)
        if s is None:
            raise ValueError(f"{t} has no sends")
        k, n = self._kind(bus)
        if k == "Aux":
            return SqAddress.LEVEL_AUX + s * 12 + n - 1
        if k == "FX":
            return SqAddress.LEVEL_FX + s * 4 + n - 1
        raise ValueError(bus)

    # --- messages ---------------------------------------------------------
    def _nrpn(self, addr, vc, vf):
        mb, lb = SqAddress.split(addr)
        s = self.status
        return bytes([s, 0x63, mb, s, 0x62, lb, s, 0x06, vc & 0x7F, s, 0x26, vf & 0x7F])

    def _set14(self, addr, value, label):
        self.c.send(self._nrpn(addr, value >> 7, value & 0x7F), label)
        mb, lb = SqAddress.split(addr)
        self.c.nrpn[(self.status & 0x0F, mb, lb)] = [value >> 7, value & 0x7F]

    def _get(self, addr):
        mb, lb = SqAddress.split(addr)
        s = self.status
        return bytes([s, 0x63, mb, s, 0x62, lb, s, 0x60, 0x7F])

    def _val(self, addr):
        mb, lb = SqAddress.split(addr)
        v = self.c.nrpn.get((self.status & 0x0F, mb, lb))
        if v is None:
            return None
        return (v[0] << 7) | (v[1] or 0)

    def fader(self, t, db):
        self._set14(self.fader_addr(t), sq_level_value(db, self.taper), f"{t} fader {db:+.1f} dB")

    def mute(self, t, on):
        self._set14(self.mute_addr(t), 1 if on else 0, f"{t} mute {'on' if on else 'off'}")

    def pan(self, t, pos):
        s = self.source(t)
        if s is None:
            return self._no("pan on outputs")
        pos = max(-1.0, min(1.0, pos))
        v = round(8191 + pos * (8192 if pos > 0 else 8191))   # 00 00 left, 3F 7F centre, 7F 7F right
        self._set14(SqAddress.LEVEL_LR + s + SqAddress.PAN_OFFSET, max(0, min(16383, v)), f"{t} pan {pos:+.2f}")

    def send_level(self, t, bus, db):
        self._set14(self.send_addr(t, bus), sq_level_value(db, self.taper), f"{t} -> {self.label(bus)} {db:+.1f} dB")

    def send_on(self, t, bus, on):
        self._set14(self.send_addr(t, bus) + SqAddress.ASSIGN_OFFSET, 1 if on else 0,
                    f"{t} -> {self.label(bus)} {'on' if on else 'off'}")

    def lr_assign(self, t, on):
        s = self.source(t)
        if s is None:
            return self._no("L+R assign on outputs")
        self._set14(SqAddress.LEVEL_LR + s + SqAddress.ASSIGN_OFFSET, 1 if on else 0, f"{t} L+R {'on' if on else 'off'}")

    def group_assign(self, t, grp, on):
        s = self.source(t)
        if s is None or s >= 48:
            return self._no("group assign from this channel")
        _, g = self._kind(grp)
        self._set14(SqAddress.GROUP_ASSIGN + s * 12 + g - 1, 1 if on else 0, f"{t} -> {grp} {'on' if on else 'off'}")

    def state(self, t):
        out = {}
        v = self._val(self.fader_addr(t))
        if v is not None:
            out["fader"] = sq_level_db(v, self.taper)
        v = self._val(self.mute_addr(t))
        if v is not None:
            out["mute"] = bool(v)
        s = self.source(t)
        if s is not None:
            v = self._val(SqAddress.LEVEL_LR + s + SqAddress.PAN_OFFSET)
            if v is not None:
                out["pan"] = (v - 8191) / 8191 if v <= 8191 else (v - 8191) / 8192
        return out

    def send_state(self, t, bus):
        try:
            a = self.send_addr(t, bus)
        except ValueError:
            return None
        lv, on = self._val(a), self._val(a + SqAddress.ASSIGN_OFFSET)
        if lv is None and on is None:
            return None
        return bool(on), sq_level_db(lv or 0, self.taper)

    def read(self, t):
        out = Unsupported.read(self, t)
        s = self.source(t)
        if s is not None:
            v = self._val(SqAddress.LEVEL_LR + s + SqAddress.ASSIGN_OFFSET)
            if v is not None:
                out["lr"] = bool(v)
            if s < 48:
                for g in self.groups():
                    v = self._val(SqAddress.GROUP_ASSIGN + s * 12 + int(g[3:]) - 1)
                    if v is not None:
                        out[f"group:{g}"] = bool(v)
        return out

    def refresh(self, targets=None, bus=None):
        if not self.c.apply:
            self.log(f"[sq dry-run] would ask the desk for {len(targets or [])} strips")
            return
        msgs = []
        for t in targets or self.targets():
            msgs.append(self._get(self.fader_addr(t)))
            msgs.append(self._get(self.mute_addr(t)))
            s = self.source(t)
            if s is not None:
                msgs.append(self._get(SqAddress.LEVEL_LR + s + SqAddress.PAN_OFFSET))
                if bus:
                    a = self.send_addr(t, bus)
                    msgs += [self._get(a), self._get(a + SqAddress.ASSIGN_OFFSET)]
        for m in msgs:
            self.c.send_quiet(m)
            time.sleep(0.003)


# ---------------------------------------------------------------------------
# dLive and Avantis: MIDI over TCP
# ---------------------------------------------------------------------------
AH_TCP_HDR = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x10, 0x01, 0x00])
# type -> (MIDI channel offset from N, first note, count) per desk
AH_TYPES = {
    "dlive": {"In": (0, 0x00, 128), "Grp": (1, 0x00, 62), "StGrp": (1, 0x40, 31), "Aux": (2, 0x00, 62),
              "StAux": (2, 0x40, 31), "Mtx": (3, 0x00, 62), "StMtx": (3, 0x40, 31), "FX": (4, 0x00, 16),
              "StFX": (4, 0x10, 16), "FXRtn": (4, 0x20, 16), "Main": (4, 0x30, 6), "DCA": (4, 0x36, 24)},
    "avantis": {"In": (0, 0x00, 96), "Grp": (1, 0x00, 54), "StGrp": (1, 0x40, 27), "Aux": (2, 0x00, 54),
                "StAux": (2, 0x40, 27), "Mtx": (3, 0x00, 54), "StMtx": (3, 0x40, 27), "FX": (4, 0x00, 12),
                "StFX": (4, 0x10, 12), "FXRtn": (4, 0x20, 12), "Main": (4, 0x30, 3), "DCA": (4, 0x36, 16)},
}
AH_COLOURS = {0: None, 1: "#c0392b", 2: "#27ae60", 3: "#d4ac0d", 4: "#2e6fd8", 5: "#8e44ad", 6: "#3fa7d6", 7: "#d0d0d0"}
AH_LABELS = {"In": "Ip", "Grp": "Grp", "StGrp": "StGrp", "Aux": "Aux", "StAux": "StAux", "Mtx": "Mtx",
             "StMtx": "StMtx", "FX": "FX", "StFX": "StFX", "FXRtn": "FXRtn", "Main": "Main", "DCA": "DCA"}


def ah_fader_value(db):
    if db <= -89.5:
        return 0
    return max(0, min(127, round((min(10.0, db) + 54.0) / 64.0 * 127)))


def ah_fader_db(v):
    return -90.0 if v <= 0 else v * 64.0 / 127 - 54.0


class AhTcpMixer(Unsupported):
    name = "ah"

    def __init__(self, client: MidiTcp, model, base_channel=1, show=None):
        self.c, self.model = client, model
        self.desk = client
        self.kind = MODELS[model]["desk"]
        self.types = AH_TYPES[self.kind]
        self.N = (base_channel - 1) & 0x0F
        # How many of each type to show: desks are configured per show, so default to a sensible subset.
        self.show = show or {"In": self.types["In"][2], "Grp": 16, "StGrp": 0, "Aux": 16, "StAux": 8, "Mtx": 4,
                             "StMtx": 4, "FX": 8, "StFX": 8, "FXRtn": 8, "Main": 2, "DCA": self.types["DCA"][2]}
        self.names, self.colours = {}, {}
        self.sends = {}  # (src chan, src note, dst chan, dst note) -> level
        client.on_sysex = self._sysex

    @property
    def log(self):
        return self.c.log

    def _split(self, t):
        m = re.match(r"([A-Za-z]+?)(\d+)$", t)
        if not m:
            raise ValueError(t)
        return m.group(1), int(m.group(2))

    def addr(self, t):
        typ, n = self._split(t)
        if typ == "Ip":
            typ = "In"
        off, first, count = self.types[typ]
        if not 1 <= n <= count:
            raise ValueError(f"{t}: {self.model} has {count}")
        return (self.N + off) & 0x0F, first + n - 1

    def _name_of(self, typ, n):
        return f"{AH_LABELS[typ]}{n}"

    def _list(self, typ):
        return [self._name_of(typ, i) for i in range(1, min(self.show.get(typ, 0), self.types[typ][2]) + 1)]

    def targets(self):
        out = []
        for typ in self.types:
            out += self._list(typ)
        return out

    def layers(self):
        out = banks(self._list("In"), 16, "In")
        for typ, title in (("Grp", "Groups"), ("StGrp", "St groups"), ("Aux", "Aux"), ("StAux", "St aux"),
                           ("Mtx", "Matrix"), ("StMtx", "St matrix"), ("FXRtn", "FX returns"), ("FX", "FX sends"),
                           ("StFX", "St FX sends"), ("DCA", "DCA")):
            lst = self._list(typ)
            if lst:
                out += banks(lst, 16, title) if len(lst) > 16 else [(title, lst)]
        if self._list("Main"):
            out.append(("Mains", self._list("Main")))
        return out

    def main_target(self):
        return "Main1"

    def label(self, t):
        return self.names.get(self.addr(t)) or t

    def colour(self, t):
        return AH_COLOURS.get(self.colours.get(self.addr(t)))

    def buses(self):
        out = []
        for typ in ("Aux", "StAux", "FX", "StFX", "Mtx", "StMtx"):
            out += [(x, x) for x in self._list(typ)]
        return out

    def is_input(self, t):
        return t.startswith(("Ip", "FXRtn"))

    def is_mono_input(self, t):
        return t.startswith("Ip")

    def is_mix(self, t):
        return t.startswith(("Grp", "StGrp", "Aux", "StAux", "Mtx", "StMtx", "FX", "StFX")) and not t.startswith("FXRtn")

    def caps(self, t):
        c = {"fader", "mute"}
        if self.is_input(t) or t.startswith(("Grp", "StGrp")):
            c.add("sends")
        return c

    def groups(self):
        return self._list("Grp") + self._list("StGrp") if self.kind == "dlive" else []

    def _sx(self, chan, *body):
        return AH_TCP_HDR + bytes([chan, *body, 0xF7])

    def fader(self, t, db):
        ch, note = self.addr(t)
        s = 0xB0 | ch
        self.c.send(bytes([s, 0x63, note, s, 0x62, 0x17, s, 0x06, ah_fader_value(db)]), f"{t} fader {db:+.1f} dB")
        self.c.nrpn[(ch, note, 0x17)] = [ah_fader_value(db), None]

    def mute(self, t, on):
        ch, note = self.addr(t)
        s = 0x90 | ch
        self.c.send(bytes([s, note, 0x7F if on else 0x3F, s, note, 0x00]), f"{t} mute {'on' if on else 'off'}")
        self.c.notes[(ch, note)] = 0x7F if on else 0x3F

    def pan(self, t, pos):
        self._no("pan")

    def send_level(self, t, bus, db):
        ch, note = self.addr(t)
        bch, bnote = self.addr(bus)
        lv = ah_fader_value(db)
        self.sends[(ch, note, bch, bnote)] = lv
        self.c.send(self._sx(ch, 0x0D, note, bch, bnote, lv), f"{t} -> {bus} {db:+.1f} dB")

    def send_on(self, t, bus, on):
        if not on:
            self.send_level(t, bus, -90.0)

    def lr_assign(self, t, on):
        ch, note = self.addr(t)
        s = 0xB0 | ch
        self.c.send(bytes([s, 0x63, note, s, 0x62, 0x18, s, 0x06, 0x7F if on else 0x3F]), f"{t} main {'on' if on else 'off'}")

    def group_assign(self, t, grp, on):
        if self.kind != "dlive":
            return self._no("group assign")
        ch, note = self.addr(t)
        gch, gnote = self.addr(grp)
        self.c.send(self._sx(ch, 0x0E, note, gch, gnote, 0x7F if on else 0x00), f"{t} -> {grp} {'on' if on else 'off'}")

    def _sysex(self, sx):
        if not sx.startswith(AH_TCP_HDR) or len(sx) < len(AH_TCP_HDR) + 3:
            return
        body = sx[len(AH_TCP_HDR):-1]
        ch, cmd, rest = body[0] & 0x0F, body[1], body[2:]
        if cmd == 0x02 and rest:
            self.names[(ch, rest[0])] = bytes(rest[1:]).decode("ascii", "replace").strip("\x00 ")
        elif cmd == 0x05 and len(rest) >= 2:
            self.colours[(ch, rest[0])] = rest[1]
        elif cmd == 0x0D and len(rest) >= 4:
            self.sends[(ch, rest[0], rest[1] & 0x0F, rest[2])] = rest[3]

    def state(self, t):
        ch, note = self.addr(t)
        out = {}
        v = self.c.nrpn.get((ch, note, 0x17))
        if v is not None:
            out["fader"] = ah_fader_db(v[0])
        vel = self.c.notes.get((ch, note))
        if vel is not None:
            out["mute"] = vel >= 0x40
        return out

    def send_state(self, t, bus):
        ch, note = self.addr(t)
        bch, bnote = self.addr(bus)
        lv = self.sends.get((ch, note, bch, bnote))
        if lv is None:
            return None
        return lv > 0, ah_fader_db(lv)

    def refresh(self, targets=None, bus=None):
        if not self.c.apply:
            self.log(f"[ah dry-run] would ask the desk for names, colours{' and levels' if self.kind == 'dlive' else ''}")
            return
        for t in targets or self.targets():
            ch, note = self.addr(t)
            msgs = [self._sx(ch, 0x01, note), self._sx(ch, 0x04, note)]
            if self.kind == "dlive":
                msgs += [self._sx(ch, 0x05, 0x0B, 0x17, note), self._sx(ch, 0x05, 0x09, note)]
                if bus and self.is_input(t):
                    bch, bnote = self.addr(bus)
                    msgs.append(self._sx(ch, 0x05, 0x0F, 0x0D, note, bch, bnote))
            for m in msgs:
                self.c.send_quiet(m)
                time.sleep(0.003)


# ---------------------------------------------------------------------------
# Yamaha RCP (DM7, DM3, QL, CL): text lines on TCP 49280
# ---------------------------------------------------------------------------
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')
YAMAHA_COLOURS = {"Blue": "#2e6fd8", "Green": "#27ae60", "Orange": "#e67e22", "Pink": "#e84393",
                  "Purple": "#8e44ad", "Red": "#c0392b", "SkyBlue": "#3fa7d6", "Yellow": "#d4ac0d",
                  "Cyan": "#16a2b8", "Magenta": "#c2185b", "White": "#d0d0d0", "Black": None, "Off": None}


def rcp_tokens(line):
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _TOKEN.finditer(line)]


def rcp_parse(line):
    """'OK get MIXER:Current/InCh/Fader/Level 0 0 -1000' / 'NOTIFY set ... 0 0 1 "ON"' / 'OK MIXER:... 0 0 5'
    -> (path, x, y, value) or None. Value is an int when it looks like one, else the string."""
    tok = rcp_tokens(line)
    if not tok or tok[0] not in ("OK", "OKm", "NOTIFY"):
        return None
    i = 1
    if i < len(tok) and tok[i] in ("get", "set"):
        i += 1
    if len(tok) < i + 4 or ":" not in tok[i]:
        return None
    path = tok[i]
    try:
        x, y = int(tok[i + 1]), int(tok[i + 2])
    except ValueError:
        return None
    raw = tok[i + 3]
    try:
        val = int(raw)
    except ValueError:
        val = raw
    return path, x, y, val


class RcpClient:
    def __init__(self, host, apply=False, log=print):
        self.apply, self.log = apply, log
        self.values = {}           # (path, x, y) -> value
        self.sock = None
        self.box = None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.got = threading.Condition()
        self.errors = 0
        if apply:
            self.sock = socket.create_connection((host, 49280), timeout=5)
            self.sock.settimeout(None)
            threading.Thread(target=self._reader, daemon=True).start()
            for line in ('scpmode encoding "utf8"', "scpmode keepalive 10000", 'scpmode valuetype "raw"'):
                self.line(line, quiet=True)
            threading.Thread(target=self._keepalive, daemon=True).start()

    def line(self, text, quiet=False):
        if not self.apply:
            if not quiet:
                self.log(f"[rcp dry-run] {text}")
            return
        with self.lock:
            self.sock.sendall((text + "\n").encode("utf-8"))
        if not quiet:
            self.log(f"[rcp sent] {text}")

    def _keepalive(self):
        while not self.stop.wait(5):
            try:
                self.line("devstatus runmode", quiet=True)
            except OSError:
                return

    def _reader(self):
        buf = b""
        try:
            while not self.stop.is_set():
                chunk = self.sock.recv(65536)
                if not chunk:
                    self.log("[rcp] the desk closed the connection")
                    return
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    self.handle(raw.decode("utf-8", "replace").rstrip("\r"))
        except OSError as e:
            if not self.stop.is_set():
                self.log(f"[rcp] connection error: {e}")

    def handle(self, line):
        if line.startswith("ERROR"):
            self.errors += 1
            with self.got:
                self.got.notify_all()
            return
        if "productname" in line and line.startswith("OK"):
            self.box = rcp_tokens(line)[-1]
        p = rcp_parse(line)
        if p:
            path, x, y, val = p
            self.values[(path, x, y)] = val
        with self.got:
            self.got.notify_all()

    def get(self, path, x, y=0, quiet=True):
        self.line(f"get {path} {x} {y}", quiet=quiet)

    def set(self, path, x, y, value):
        v = f'"{value}"' if isinstance(value, str) else str(value)
        self.values[(path, x, y)] = value
        self.line(f"set {path} {x} {y} {v}")

    def ask(self, path, x, y=0, timeout=0.6):
        """Synchronous get: returns the value, or None on ERROR / no answer."""
        if not self.apply:
            return None
        key = (path, x, y)
        self.values.pop(key, None)
        errs = self.errors
        self.get(path, x, y)
        end = time.time() + timeout
        with self.got:
            while key not in self.values and self.errors == errs and time.time() < end:
                self.got.wait(0.05)
        return self.values.get(key)

    def close(self):
        self.stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


def rcp_level(db):
    return -32768 if db <= -89.5 else round(max(-138.0, min(10.0, db)) * 100)


def rcp_db(v):
    if not isinstance(v, int) or v <= -13800:
        return -90.0
    return v / 100.0


class RcpMixer(Unsupported):
    name = "rcp"
    KIND_LABEL = {"InCh": "Ch", "StInCh": "StIn", "Mix": "Mix", "Mtrx": "Mtx", "St": "St", "Mono": "Mono", "DCA": "DCA"}

    def __init__(self, client: RcpClient, model):
        self.c, self.model = client, model
        self.desk = client
        self.counts = dict(MODELS[model]["counts"])

    @property
    def log(self):
        return self.c.log

    def detect_counts(self):
        """Ask the desk how many of each channel type it has (binary search on Fader/Level gets)."""
        if not self.c.apply:
            return self.counts
        for kind, guess in list(self.counts.items()):
            path = f"MIXER:Current/{kind}/Fader/Level"
            if self.c.ask(path, 0) is None:
                self.counts[kind] = 0
                continue
            lo, hi = 1, max(guess, 1) * 2 + 1   # lo exists, hi assumed not to
            if self.c.ask(path, guess - 1) is not None and self.c.ask(path, guess) is None:
                continue  # the model default is right
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if self.c.ask(path, mid - 1) is not None:
                    lo = mid
                else:
                    hi = mid
            self.counts[kind] = lo
        return self.counts

    @staticmethod
    def parse(t):
        kind, _, idx = t.partition(":")
        return kind, int(idx)

    def _list(self, kind):
        return [f"{kind}:{i}" for i in range(self.counts.get(kind, 0))]

    def targets(self):
        out = []
        for k in ("InCh", "StInCh", "Mix", "Mtrx", "St", "Mono", "DCA"):
            out += self._list(k)
        return out

    def layers(self):
        out = banks(self._list("InCh"), 16, "Ch")
        if self.counts.get("StInCh"):
            out.append(("ST in", self._list("StInCh")))
        mixes = self._list("Mix")
        out += banks(mixes, 16, "Mix") if len(mixes) > 16 else [("Mixes", mixes)]
        out.append(("Matrix", self._list("Mtrx")))
        dcas = self._list("DCA")
        out += banks(dcas, 16, "DCA") if len(dcas) > 16 else [("DCA", dcas)]
        return [(n, l) for n, l in out if l]

    def main_target(self):
        return "St:0"

    def label(self, t):
        kind, x = self.parse(t)
        name = self.c.values.get((f"MIXER:Current/{kind}/Label/Name", x, 0))
        return name if isinstance(name, str) and name.strip() else f"{self.KIND_LABEL.get(kind, kind)} {x + 1}"

    def short(self, t):
        kind, x = self.parse(t)
        return f"{self.KIND_LABEL.get(kind, kind)} {x + 1}"

    def colour(self, t):
        kind, x = self.parse(t)
        return YAMAHA_COLOURS.get(self.c.values.get((f"MIXER:Current/{kind}/Label/Color", x, 0)))

    def buses(self):
        return [(m, f"Mix {int(m.split(':')[1]) + 1}") for m in self._list("Mix")]

    def mixes(self):
        return self._list("Mix")

    def is_input(self, t):
        return t.startswith(("InCh", "StInCh"))

    def is_mono_input(self, t):
        return t.startswith("InCh")

    def is_mix(self, t):
        return t.startswith(("Mix", "Mtrx"))

    def caps(self, t):
        c = {"fader", "mute"}
        if self.is_input(t):
            c |= {"pan", "sends", "eq", "hpf"}
        if t.startswith("InCh") and self.model.startswith("Yamaha DM"):
            c.add("preamp")
        if self.is_mix(t) or t.startswith("St"):
            c.add("eq")
        return c

    def _p(self, t, path):
        kind, x = self.parse(t)
        return f"MIXER:Current/{kind}/{path}", x

    def fader(self, t, db):
        p, x = self._p(t, "Fader/Level")
        self.c.set(p, x, 0, rcp_level(db))

    def mute(self, t, on):
        p, x = self._p(t, "Fader/On")
        self.c.set(p, x, 0, 0 if on else 1)

    def pan(self, t, pos):
        if not self.is_input(t):
            return self._no("pan on this strip")
        p, x = self._p(t, "ToSt/Pan")
        self.c.set(p, x, 0, round(max(-1.0, min(1.0, pos)) * 63))

    def send_level(self, t, bus, db):
        p, x = self._p(t, "ToMix/Level")
        self.c.set(p, x, self.parse(bus)[1], rcp_level(db))

    def send_on(self, t, bus, on):
        p, x = self._p(t, "ToMix/On")
        self.c.set(p, x, self.parse(bus)[1], 1 if on else 0)

    def send(self, t, mix, on, db, post=True):
        self.send_on(t, mix, on)
        p, x = self._p(t, "ToMix/PrePost")
        self.c.set(p, x, self.parse(mix)[1], 0 if post else 1)   # ASSUMED: 0 = post, 1 = pre
        self.send_level(t, mix, db)

    def lr_assign(self, t, on):
        p, x = self._p(t, "ToSt/On")
        self.c.set(p, x, 0, 1 if on else 0)

    def gain(self, t, db):   # DM7 OSC spec: InCh/Port/HA/Gain -600..6600 (dB x 100)
        if "preamp" not in self.caps(t):
            return self._no("preamp gain")
        p, x = self._p(t, "Port/HA/Gain")
        self.c.set(p, x, 0, round(max(-6.0, min(66.0, db)) * 100))

    def hpf(self, t, on, hz):
        p, x = self._p(t, "HPF/On")
        self.c.set(p, x, 0, 1 if on else 0)
        p, x = self._p(t, "HPF/Freq")
        self.c.set(p, x, 0, round(min(2000, max(20, hz)) * 10))

    def peq_in(self, t, on):
        p, x = self._p(t, "PEQ/On")
        self.c.set(p, x, 0, 1 if on else 0)

    def peq_band(self, t, band, gain_db, hz, width_oct):
        from .notches import octaves_to_q
        kind, x = self.parse(t)
        base = f"MIXER:Current/{kind}/PEQ/Band"
        self.c.set(f"{base}/Freq", x, band - 1, round(min(20000, max(20, hz)) * 10))
        self.c.set(f"{base}/Q", x, band - 1, round(min(16.0, max(0.1, octaves_to_q(width_oct))) * 1000))
        self.c.set(f"{base}/Gain", x, band - 1, round(max(-18, min(18, gain_db)) * 100))

    def state(self, t):
        kind, x = self.parse(t)
        v = self.c.values
        out = {}
        lv = v.get((f"MIXER:Current/{kind}/Fader/Level", x, 0))
        if lv is not None:
            out["fader"] = rcp_db(lv)
        on = v.get((f"MIXER:Current/{kind}/Fader/On", x, 0))
        if on is not None:
            out["mute"] = on == 0
        pan = v.get((f"MIXER:Current/{kind}/ToSt/Pan", x, 0))
        if isinstance(pan, int):
            out["pan"] = pan / 63
        return out

    def send_state(self, t, bus):
        kind, x = self.parse(t)
        y = self.parse(bus)[1]
        lv = self.c.values.get((f"MIXER:Current/{kind}/ToMix/Level", x, y))
        on = self.c.values.get((f"MIXER:Current/{kind}/ToMix/On", x, y))
        if lv is None and on is None:
            return None
        return bool(on), rcp_db(lv)

    def read(self, t):
        out = Unsupported.read(self, t)
        kind, x = self.parse(t)
        st = self.c.values.get((f"MIXER:Current/{kind}/ToSt/On", x, 0))
        if st is not None:
            out["lr"] = bool(st)
        return out

    def refresh(self, targets=None, bus=None):
        if not self.c.apply:
            self.log(f"[rcp dry-run] would read {len(targets or [])} strips from the desk")
            return
        for t in targets or self.targets():
            kind, x = self.parse(t)
            paths = ["Fader/Level", "Fader/On", "Label/Name", "Label/Color"]
            if self.is_input(t):
                paths += ["ToSt/Pan", "ToSt/On"]
            for p in paths:
                self.c.get(f"MIXER:Current/{kind}/{p}", x, 0)
            if bus and self.is_input(t):
                y = self.parse(bus)[1]
                self.c.get(f"MIXER:Current/{kind}/ToMix/Level", x, y)
                self.c.get(f"MIXER:Current/{kind}/ToMix/On", x, y)
            time.sleep(0.004)


# ---------------------------------------------------------------------------
def make_mixer(model, host, apply, log, shared_qu=None, midi_channel=1, taper="linear"):
    """Builds (controller, connection to close or None) for a model name from MODELS."""
    spec = MODELS[model]
    fam = spec["family"]
    if fam == "qu":
        from .desks import QuDesk
        if shared_qu is not None:
            return QuMixer(shared_qu, model), None
        d = QuDesk(host, "Ip1", midi_channel=midi_channel, apply=apply, log=log)
        if apply:
            d.check_link()
        return QuMixer(d, model), d
    if fam == "sq":
        c = MidiTcp(host, apply=apply, log=log, tag="sq")
        return SqMixer(c, model, midi_channel=midi_channel, taper=taper), c
    if fam == "ah":
        c = MidiTcp(host, apply=apply, log=log, tag=MODELS[model]["desk"])
        return AhTcpMixer(c, model, base_channel=midi_channel), c
    c = RcpClient(host, apply=apply, log=log)
    m = RcpMixer(c, model)
    if apply:
        c.line("devinfo productname", quiet=True)
        m.detect_counts()
    return m, c
