#!/usr/bin/env python3
"""Read-only probe for an Allen & Heath Qu-16/24/32/Pac/SB.

Python 3.8+, standard library only. Nothing here changes a mixer setting.

Commands
  discover            Broadcast A&H "QU Find" on UDP 51320 (the discovery string
                      Mixing Station uses) and print whatever answers. Experimental.
  sync HOST           Connect to the PUBLIC MIDI-over-TCP port (51325), ask for the
                      full system state, decode everything the desk pushes, fetch
                      channel names, and keep logging live changes.
  meters HOST         Same connection, but turn metering on and print input meters.
  listen-private HOST Open the PRIVATE Qu-Pad port (TCP 51326) and only hex-dump
                      what the desk sends unprompted. Sends nothing. Experimental.

Notes
  * The Qu allows ONE MIDI-over-TCP client at a time. Close the A&H MIDI Control
    app / any DAW link before `sync`.
  * Output: a human log on stdout, plus JSON-lines of every raw message in
    qu16-<command>-<time>.jsonl next to where you run it.
"""
import argparse
import json
import socket
import sys
import threading
import time

PUBLIC_PORT = 51325
PRIVATE_PORT = 51326
DISCOVERY_PORT = 51320

AH_HDR = bytes([0xF0, 0x00, 0x00, 0x1A, 0x50, 0x11, 0x01, 0x00])
BOX_IDS = {1: "Qu-16", 2: "Qu-24", 3: "Qu-32", 4: "Qu-Pac", 5: "Qu-SB"}

# Channel address (NRPN MSB / mute note) -> name, from Qu MIDI Protocol V1.9
def ch_name(ch):
    if 0x00 <= ch <= 0x03: return f"FXSend{ch + 1}"
    if 0x08 <= ch <= 0x0B: return f"FXRtn{ch - 0x07}"
    if 0x10 <= ch <= 0x13: return f"DCA{ch - 0x0F}"
    if 0x20 <= ch <= 0x3F: return f"Ip{ch - 0x1F}"
    if 0x40 <= ch <= 0x42: return f"ST{ch - 0x3F}"
    if 0x50 <= ch <= 0x53: return f"MuteGrp{ch - 0x4F}"
    if 0x60 <= ch <= 0x63: return f"Mix{ch - 0x5F}"
    if ch == 0x64: return "Mix5-6"
    if ch == 0x65: return "Mix7-8"
    if ch == 0x66: return "Mix9-10"
    if ch == 0x67: return "LR"
    if 0x68 <= ch <= 0x6B: return f"Grp{(ch - 0x68) * 2 + 1}-{(ch - 0x68) * 2 + 2}"
    if 0x6C <= ch <= 0x6D: return f"Mtx{(ch - 0x6C) * 2 + 1}-{(ch - 0x6C) * 2 + 2}"
    return f"ch0x{ch:02X}"

PARAM_IDS = {
    0x01: "PEQ LF gain", 0x02: "PEQ LF freq", 0x03: "PEQ LF width", 0x04: "PEQ LF type",
    0x05: "PEQ LM gain", 0x06: "PEQ LM freq", 0x07: "PEQ LM width",
    0x09: "PEQ HM gain", 0x0A: "PEQ HM freq", 0x0B: "PEQ HM width",
    0x0D: "PEQ HF gain", 0x0E: "PEQ HF freq", 0x0F: "PEQ HF width", 0x10: "PEQ HF type",
    0x11: "PEQ in", 0x12: "USB source", 0x13: "HPF freq", 0x14: "HPF in",
    0x16: "Pan", 0x17: "Fader", 0x18: "LR assign", 0x19: "Preamp gain",
    0x20: "Send level", 0x40: "DCA assign",
    0x41: "Gate attack", 0x42: "Gate release", 0x43: "Gate hold", 0x44: "Gate thresh",
    0x45: "Gate depth", 0x46: "Gate in",
    0x48: "FX delay coarse", 0x49: "FX delay fine",
    0x50: "Pre/post", 0x51: "PAFL", 0x52: "Digital trim", 0x54: "Stereo trim",
    0x55: "Mix assign", 0x57: "Preamp source", 0x58: "dSNAKE gain", 0x59: "dSNAKE pad",
    0x5A: "dSNAKE 48V", 0x5C: "MuteGrp assign", 0x5D: "dSNAKE patch", 0x5E: "Group mode",
    0x61: "Comp type", 0x62: "Comp attack", 0x63: "Comp release", 0x64: "Comp knee",
    0x65: "Comp ratio", 0x66: "Comp thresh", 0x67: "Comp gain", 0x68: "Comp in",
    0x69: "48V", 0x6A: "Polarity", 0x6B: "Insert in", 0x6C: "Delay", 0x6D: "Delay in",
    0x70: "GEQ gain", 0x71: "GEQ in",
}

# Fader/send 7-bit value -> dB, key points from the V1.9 doc (verify on your desk).
FADER_POINTS = [(0x00, float("-inf")), (0x0C, -45), (0x10, -40), (0x17, -35), (0x1F, -30),
                (0x27, -25), (0x2F, -20), (0x36, -15), (0x3F, -10), (0x4F, -5),
                (0x62, 0), (0x72, 5), (0x7F, 10)]

def fader_db(v):
    if v <= 0:
        return float("-inf")
    for (v0, d0), (v1, d1) in zip(FADER_POINTS, FADER_POINTS[1:]):
        if v0 <= v <= v1:
            if d0 == float("-inf"):
                d0 = -60.0  # approximate below the first documented point
            return d0 + (d1 - d0) * (v - v0) / (v1 - v0)
    return 10.0

def hexs(b):
    return " ".join(f"{x:02X}" for x in b)


class Log:
    def __init__(self, command):
        self.path = f"qu16-{command}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        self.f = open(self.path, "a", encoding="utf-8")
        print(f"[log] raw messages -> {self.path}")

    def raw(self, kind, data, **extra):
        rec = {"t": round(time.time(), 3), "kind": kind, "hex": hexs(data)}
        rec.update(extra)
        self.f.write(json.dumps(rec) + "\n")
        self.f.flush()


class MidiParser:
    """Turns a TCP byte stream into MIDI messages (handles running status and SysEx)."""

    def __init__(self, on_msg):
        self.on_msg = on_msg
        self.status = None
        self.data = []
        self.sysex = None

    def feed(self, chunk):
        for b in chunk:
            if self.sysex is not None:
                if b == 0xF7:
                    self.sysex.append(b)
                    self.on_msg(bytes(self.sysex))
                    self.sysex = None
                elif b >= 0xF8:
                    self.on_msg(bytes([b]))
                elif b & 0x80:
                    self.sysex = None  # aborted SysEx
                    self._status(b)
                else:
                    self.sysex.append(b)
                continue
            if b == 0xF0:
                self.sysex = bytearray([b])
            elif b >= 0xF8:
                self.on_msg(bytes([b]))  # realtime, incl. FE active sensing
            elif b & 0x80:
                self._status(b)
            elif self.status is not None:
                self.data.append(b)
                need = 1 if (self.status & 0xF0) in (0xC0, 0xD0) else 2
                if len(self.data) == need:
                    self.on_msg(bytes([self.status] + self.data))
                    self.data = []

    def _status(self, b):
        self.status = b
        self.data = []


class QuState:
    def __init__(self, log):
        self.log = log
        self.nrpn = {}  # per midi channel: {"msb","lsb","val"}
        self.params = {}  # (ch, id, vx) -> va
        self.mutes = {}
        self.names = {}
        self.box = None
        self.synced = threading.Event()
        self.verbose = True

    def handle(self, msg):
        st = msg[0]
        if st == 0xFE:
            return  # active sensing
        if st == 0xF0:
            self.log.raw("sysex", msg)
            self.sysex(msg)
            return
        kind = st & 0xF0
        chan = st & 0x0F
        self.log.raw("midi", msg)
        if kind == 0xB0:
            cc, val = msg[1], msg[2]
            n = self.nrpn.setdefault(chan, {})
            if cc == 0x63:
                n.clear(); n["msb"] = val
            elif cc == 0x62:
                n["lsb"] = val
            elif cc == 0x06:
                n["val"] = val
                n["vx"] = None
                self._emit_nrpn(chan, n, complete=False)
            elif cc == 0x26:
                n["vx"] = val
                self._emit_nrpn(chan, n, complete=True)
            else:
                self.say(f"CC ch{chan + 1} {cc:02X}={val:02X}")
        elif kind == 0x90:
            note, vel = msg[1], msg[2]
            if vel == 0:
                return
            on = vel >= 0x40
            self.mutes[note] = on
            self.say(f"MUTE {ch_name(note):10s} {'ON' if on else 'off'}")
        elif kind == 0xC0:
            self.say(f"SCENE recall program {msg[1]} (scene {msg[1] + 1})")
        else:
            self.say(f"MIDI {hexs(msg)}")

    def _emit_nrpn(self, chan, n, complete):
        # Params that use an index send BN 26 VX after BN 06; others may not.
        # We print on 06 and again on 26 if it arrives, keeping the last.
        if "msb" not in n or "lsb" not in n or "val" not in n:
            return
        ch, pid, va, vx = n["msb"], n["lsb"], n["val"], n.get("vx")
        if not complete:
            self.params[(ch, pid, None)] = va
            return
        self.params.pop((ch, pid, None), None)
        self.params[(ch, pid, vx)] = va
        label = PARAM_IDS.get(pid, f"id0x{pid:02X}")
        extra = ""
        if pid in (0x17, 0x20):
            extra = f" ({fader_db(va):.1f} dB)"
        self.say(f"NRPN {ch_name(ch):10s} {label:16s} va={va:02X} vx={vx:02X}{extra}")

    def sysex(self, msg):
        if not msg.startswith(AH_HDR) or len(msg) < len(AH_HDR) + 3:
            self.say(f"SYSEX (other) {hexs(msg)}")
            return
        body = msg[len(AH_HDR):-1]
        dev, cmd, rest = body[0], body[1], body[2:]
        if cmd == 0x11 and len(rest) >= 1:
            self.box = BOX_IDS.get(rest[0], f"box{rest[0]}")
            ver = ".".join(str(x) for x in rest[1:])
            self.say(f"SYSTEM STATE reply: {self.box}, firmware {ver}")
        elif cmd == 0x14:
            self.say("END OF SYNC")
            self.synced.set()
        elif cmd == 0x02 and rest:
            name = bytes(rest[1:]).decode("ascii", "replace").rstrip("\x00 ")
            self.names[rest[0]] = name
            self.say(f"NAME {ch_name(rest[0]):10s} '{name}'")
        elif cmd == 0x13:
            self.meters(rest)
        else:
            self.say(f"SYSEX cmd {cmd:02X} dev {dev:02X}: {hexs(rest)}")

    def meters(self, packed):
        data = unpack7(packed)
        vals = []
        for i in range(0, len(data) - 1, 2):
            raw = (data[i] << 8) | data[i + 1]
            vals.append((raw - 0x8000) / 256.0)
        # Qu-16 layout per the doc: 16 mono input blocks of 10 meters first.
        # Meter 0 of each block = post-preamp.
        ins = [vals[b * 10] for b in range(16) if b * 10 < len(vals)]
        print("\rIN " + " ".join(f"{v:6.1f}" for v in ins), end="", flush=True)

    def say(self, text):
        if self.verbose:
            print(text, flush=True)


def unpack7(packed):
    """A&H 7-bit packing: each group of up to 8 bytes = 1 MSB byte + up to 7 data bytes."""
    out = bytearray()
    i = 0
    while i < len(packed):
        msbs = packed[i]
        group = packed[i + 1:i + 8]
        for j, b in enumerate(group):
            bit = (msbs >> (6 - j)) & 1  # bit 6 of the MSB byte belongs to the first data byte
            out.append(b | (bit << 7))
        i += 8
    return bytes(out)


def sysex(dev, cmd, *payload):
    return AH_HDR + bytes([dev, cmd, *payload, 0xF7])


def open_public(host, log):
    s = socket.create_connection((host, PUBLIC_PORT), timeout=5)
    s.settimeout(None)
    print(f"[tcp] connected to {host}:{PUBLIC_PORT}")
    stop = threading.Event()

    def keepalive():
        while not stop.is_set():
            try:
                s.sendall(b"\xFE")
            except OSError:
                return
            time.sleep(0.3)

    threading.Thread(target=keepalive, daemon=True).start()
    return s, stop


def reader(sock, parser, log, stop):
    try:
        while not stop.is_set():
            chunk = sock.recv(4096)
            if not chunk:
                print("\n[tcp] desk closed the connection")
                break
            parser.feed(chunk)
    except OSError as e:
        print(f"\n[tcp] {e}")
    stop.set()


def cmd_sync(args):
    log = Log("sync")
    state = QuState(log)
    sock, stop = open_public(args.host, log)
    parser = MidiParser(state.handle)
    threading.Thread(target=reader, args=(sock, parser, log, stop), daemon=True).start()

    # Get System State, all-call. iPadFlag 0 = not a Qu-Pad client.
    sock.sendall(sysex(0x7F, 0x10, 0x00))
    if not state.synced.wait(20):
        print("[warn] no end-of-sync seen within 20 s; carrying on")

    for ch in list(range(0x20, 0x40)) + [0x40, 0x41, 0x42] + list(range(0x60, 0x68)) + list(range(0x10, 0x14)):
        sock.sendall(sysex(0x7F, 0x01, ch))
        time.sleep(0.02)
    time.sleep(1)

    snap = {
        "box": state.box,
        "names": {ch_name(k): v for k, v in state.names.items()},
        "mutes": {ch_name(k): v for k, v in state.mutes.items()},
        "params": [{"ch": ch_name(c), "id": f"{p:02X}", "param": PARAM_IDS.get(p), "vx": vx, "va": va}
                   for (c, p, vx), va in sorted(state.params.items(), key=lambda x: (x[0][0], x[0][1], x[0][2] or 0))],
    }
    out = f"qu16-snapshot-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1)
    print(f"[ok] snapshot of {len(snap['params'])} parameters -> {out}")

    if args.seconds:
        print(f"[live] logging changes for {args.seconds} s - move some faders on the desk")
        stop.wait(args.seconds)
    stop.set()
    sock.close()


def cmd_meters(args):
    log = Log("meters")
    state = QuState(log)
    state.verbose = False
    sock, stop = open_public(args.host, log)
    parser = MidiParser(state.handle)
    threading.Thread(target=reader, args=(sock, parser, log, stop), daemon=True).start()
    sock.sendall(sysex(0x7F, 0x12, 0x01))
    try:
        stop.wait(args.seconds)
    finally:
        try:
            sock.sendall(sysex(0x7F, 0x12, 0x00))
        except OSError:
            pass
        stop.set()
        sock.close()
    print()


def cmd_discover(args):
    log = Log("discover")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", 0))
    s.settimeout(0.5)
    probes = [b"QU Find", b"QU Find\x00"]
    for p in probes:
        s.sendto(p, (args.broadcast, DISCOVERY_PORT))
        log.raw("sent", p)
    print(f"[udp] sent 'QU Find' to {args.broadcast}:{DISCOVERY_PORT}, listening 5 s")
    end = time.time() + 5
    while time.time() < end:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        log.raw("reply", data, src=f"{addr[0]}:{addr[1]}")
        print(f"{addr[0]}:{addr[1]}  {data!r}")
        print(f"   hex: {hexs(data)}")


def cmd_listen_private(args):
    log = Log("private")
    s = socket.create_connection((args.host, PRIVATE_PORT), timeout=5)
    s.settimeout(1)
    print(f"[tcp] connected to {args.host}:{PRIVATE_PORT}; sending nothing, dumping for {args.seconds} s")
    end = time.time() + args.seconds
    while time.time() < end:
        try:
            data = s.recv(4096)
        except socket.timeout:
            continue
        if not data:
            print("[tcp] desk closed the connection")
            break
        log.raw("recv", data)
        print(hexs(data))
    s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover"); d.add_argument("--broadcast", default="255.255.255.255")
    p = sub.add_parser("sync"); p.add_argument("host"); p.add_argument("--seconds", type=int, default=60)
    m = sub.add_parser("meters"); m.add_argument("host"); m.add_argument("--seconds", type=int, default=20)
    l = sub.add_parser("listen-private"); l.add_argument("host"); l.add_argument("--seconds", type=int, default=15)
    args = ap.parse_args()
    {"discover": cmd_discover, "sync": cmd_sync, "meters": cmd_meters,
     "listen-private": cmd_listen_private}[args.cmd](args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
