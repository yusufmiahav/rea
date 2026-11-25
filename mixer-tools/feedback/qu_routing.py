"""Qu-16/24/32 routing over MIDI (Qu MIDI Protocol V1.9): send input channels to a mix,
take them off L+R, and read back what the desk reports.

Every parameter is an NRPN:  BN 63 CH  BN 62 ID  BN 06 VA  BN 26 VX   (checked against the
Qu MIDI Protocol V1.9+ Iss.2 PDF, pages 5-6 and the reference table):
  * Send level   ID 20, VX 00-06 = Mix1, Mix2, Mix3, Mix4, Mix5-6, Mix7-8, Mix9-10
  * Pre/post     ID 50, VX as above, VA 00 = post-fade, 01 = pre-fade
  * Mix assign   ID 55, VX as above, VA 01 = on, 00 = off
  * LR assign    ID 18, VX 07, VA 01 = on, 00 = off
  * Fader        ID 17, VX 07 (a mix master is addressed by its channel code, Mix1 = 60)
The "Read from desk" button asks for the system state. The desk then reports every parameter as
NRPN, so the page can show the real routing and learn the exact VX the desk uses.
"""
from __future__ import annotations

ID_FADER, ID_LR, ID_SEND, ID_PREPOST, ID_ASSIGN = 0x17, 0x18, 0x20, 0x50, 0x55

MIX_CODES = {"Mix1": 0x60, "Mix2": 0x61, "Mix3": 0x62, "Mix4": 0x63,       # channel codes (CH)
             "Mix5-6": 0x64, "Mix7-8": 0x65, "Mix9-10": 0x66}
MIX_VX = {name: code - 0x60 for name, code in MIX_CODES.items()}            # send index (VX) 00-06
LR_VX = 0x07


def input_names(n_mono=32):
    return [f"Ip{i}" for i in range(1, n_mono + 1)] + ["ST1", "ST2", "ST3"]


class NrpnParser:
    """Turns the desk's MIDI byte stream into (ch, id, va, vx) parameter values.
    Copes with running status and skips SysEx and real-time bytes."""

    def __init__(self):
        self.params = {}        # (ch, id, vx) -> va
        self.vx_seen = {}       # id -> set of vx values the desk used
        self.mutes = {}         # channel code -> True when muted (note on messages)
        self._status = None
        self._data = []
        self._in_sysex = False
        self._cur = {}          # per MIDI channel: {"ch":..,"id":..,"va":..}

    def feed(self, data: bytes):
        for b in data:
            if b == 0xF0:
                self._in_sysex = True
                continue
            if self._in_sysex:
                if b == 0xF7:
                    self._in_sysex = False
                continue
            if b >= 0xF8 or b == 0xFE:
                continue                     # real-time / active sensing
            if b & 0x80:
                self._status, self._data = b, []
                continue
            if self._status is None:
                continue
            self._data.append(b)
            if len(self._data) == 2:
                if self._status & 0xF0 == 0xB0:
                    self._cc(self._status & 0x0F, *self._data)
                elif self._status & 0xF0 == 0x90 and self._data[1] > 0:
                    self.mutes[self._data[0]] = self._data[1] >= 0x40
                self._data = []              # running status: keep _status

    def _cc(self, midi_ch, cc, val):
        cur = self._cur.setdefault(midi_ch, {})
        if cc == 0x63:
            cur["ch"] = val
        elif cc == 0x62:
            cur["id"] = val
        elif cc == 0x06:
            cur["va"] = val
        elif cc == 0x26 and {"ch", "id", "va"} <= cur.keys():
            self.params[(cur["ch"], cur["id"], val)] = cur["va"]
            self.vx_seen.setdefault(cur["id"], set()).add(val)

    def get(self, ch, pid, vx=None):
        if vx is not None:
            return self.params.get((ch, pid, vx))
        for (c, i, _), va in self.params.items():
            if c == ch and i == pid:
                return va
        return None

    def lr_vx(self):
        return LR_VX


def route_to_mix(desk, inputs, mix, send_db=0.0, post_fade=True, off_lr=True, lr_vx=LR_VX, fader_value=None,
                 others=()):
    """Sends each input to the mix (assign on, post- or pre-fade, send level) and, if asked,
    takes it off L+R. Every channel in `others` is taken off the mix (assign off).
    `desk` is a QuDesk (or a view of one); returns the number of messages."""
    from .desks import qu_fader_value
    vx = MIX_VX[mix]
    level = qu_fader_value(send_db) if fader_value is None else fader_value
    count = 0
    for name in inputs:
        v = desk.view(name)
        msgs = [v.nrpn(ID_ASSIGN, 0x01, vx), v.nrpn(ID_PREPOST, 0x00 if post_fade else 0x01, vx),
                v.nrpn(ID_SEND, level, vx)]
        label = f"-> {mix} {'post' if post_fade else 'pre'}-fade {send_db:+.1f} dB"
        if off_lr:
            msgs.append(v.nrpn(ID_LR, 0x00, lr_vx))
            label += ", off L+R"
        v._send(msgs, label)
        count += len(msgs)
    for name in others:
        v = desk.view(name)
        v._send([v.nrpn(ID_ASSIGN, 0x00, vx)], f"off {mix}")
        count += 1
    return count


def restore_to_lr(desk, inputs, mix, lr_vx=LR_VX):
    """Undo: back on L+R and no longer assigned to the mix."""
    vx = MIX_VX[mix]
    for name in inputs:
        v = desk.view(name)
        v._send([v.nrpn(ID_LR, 0x01, lr_vx), v.nrpn(ID_ASSIGN, 0x00, vx)], f"back on L+R, off {mix}")


def set_return(desk, ret, on_lr=True, lr_vx=LR_VX, fader_db=0.0):
    """The channel the mix comes back on: on L+R at a set fader level."""
    from .desks import qu_fader_value
    v = desk.view(ret)
    msgs = [v.nrpn(ID_LR, 0x01 if on_lr else 0x00, lr_vx), v.nrpn(ID_FADER, qu_fader_value(fader_db))]
    v._send(msgs, f"return: {'on' if on_lr else 'off'} L+R, fader {fader_db:+.1f} dB")
