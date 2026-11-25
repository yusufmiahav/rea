#!/usr/bin/env python3
"""Probe for a Yamaha DM7 (also DM3, TF, Rivage, CL/QL where RCP is enabled) over the
text remote control protocol (RCP) on TCP 49280.

Python 3.8+, standard library only. It is read-only unless you use the `set` command.

Commands
  info HOST            Set the session mode the way Mixing Station does (utf8, keepalive,
                       raw values), print device info, then try the undocumented
                       enumeration commands (prminfo / mtrinfo) and report what answers.
  dump HOST            `get` the main parameters for every channel/mix/matrix/DCA and
                       save a JSON snapshot.
  watch HOST           Print every NOTIFY the desk sends. Move things on the desk and
                       this is how you discover parameter addresses.
  get HOST ADDR X Y    One get, e.g.  get 192.168.0.128 MIXER:Current/InCh/Fader/Level 0 0
  set HOST ADDR X Y V  One set (this DOES change the desk).
  raw HOST "LINE"      Send any single line and print the replies (for exploring).
  multicast            Listen on 239.192.0.164:50240 (the group Mixing Station joins for
                       Yamaha discovery) and print what arrives. Experimental.

Every line sent and received is also written to dm7-<command>-<time>.log.
"""
import argparse
import json
import socket
import struct
import sys
import threading
import time

PORT = 49280

# Counts for the DM7 (DM7 Compact: 72 inputs). Taken from the DM7 OSC spec V1.1.0.
COUNTS = {"InCh": 120, "Mix": 48, "Mtrx": 12, "St": 4, "DCA": 24}

DUMP_PATHS = {
    "InCh": ["Fader/Level", "Fader/On", "Label/Name", "Label/Color", "ToSt/Pan", "Port/HA/Gain"],
    "Mix": ["Fader/Level", "Fader/On", "Label/Name", "Label/Color"],
    "Mtrx": ["Fader/Level", "Fader/On", "Label/Name", "Label/Color"],
    "St": ["Fader/Level", "Fader/On", "Label/Name"],
    "DCA": ["Fader/Level", "Fader/On", "Label/Name", "Label/Color"],
}


class Rcp:
    def __init__(self, host, logname, echo=True):
        self.sock = socket.create_connection((host, PORT), timeout=5)
        self.sock.settimeout(None)
        self.log = open(f"dm7-{logname}-{time.strftime('%Y%m%d-%H%M%S')}.log", "a", encoding="utf-8")
        self.echo = echo
        self.lines = []
        self.cv = threading.Condition()
        self.closed = False
        self.on_notify = None
        threading.Thread(target=self._reader, daemon=True).start()
        print(f"[tcp] connected to {host}:{PORT}  (log: {self.log.name})")

    def _reader(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                with self.cv:
                    self.closed = True
                    self.cv.notify_all()
                print("[tcp] connection closed")
                return
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                self.log.write(f"{time.time():.3f} < {line}\n"); self.log.flush()
                if line.startswith("NOTIFY") and self.on_notify:
                    self.on_notify(line)
                    continue
                with self.cv:
                    self.lines.append(line)
                    self.cv.notify_all()

    def send(self, line):
        self.log.write(f"{time.time():.3f} > {line}\n"); self.log.flush()
        if self.echo:
            print(f"> {line}")
        self.sock.sendall((line + "\n").encode("utf-8"))

    def request(self, line, timeout=2.0, multi_quiet=0.3):
        """Send one line; collect replies until the line goes quiet. Returns list of lines."""
        with self.cv:
            self.lines.clear()
        self.send(line)
        got = []
        deadline = time.time() + timeout
        with self.cv:
            while time.time() < deadline and not self.closed:
                if self.lines:
                    got.extend(self.lines); self.lines.clear()
                    # OK/ERROR usually ends it; allow a short tail for multi-line answers
                    deadline = min(deadline, time.time() + multi_quiet)
                self.cv.wait(0.05)
            got.extend(self.lines); self.lines.clear()
        if self.echo:
            for g in got:
                print(f"< {g}")
            if not got:
                print("< (no reply)")
        return got

    def setup_session(self):
        # Same three settings Mixing Station uses on connect.
        self.request('scpmode encoding "utf8"')
        self.request("scpmode keepalive 10000")
        self.request('scpmode valuetype "raw"')
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _keepalive(self):
        # Any traffic counts; a cheap devstatus every 5 s keeps the 10 s keepalive happy.
        while not self.closed:
            time.sleep(5)
            try:
                self.sock.sendall(b"devstatus runmode\n")
            except OSError:
                return


def parse_value(reply, addr):
    """'OK get MIXER:... 0 0 -1000' or 'OK MIXER:... 0 0 -1000' -> '-1000' (string kept raw)."""
    for line in reply:
        if line.startswith("OK") and addr in line:
            tail = line.split(addr, 1)[1].strip().split(" ", 2)
            if len(tail) >= 3:
                return tail[2]
        if line.startswith("ERROR"):
            return {"error": line}
    return None


def cmd_info(a):
    r = Rcp(a.host, "info")
    r.setup_session()
    for item in ["productname", "version", "protocolver", "paramsetver", "serialno", "deviceid"]:
        r.request(f"devinfo {item}")
    r.request("devstatus runmode")
    print("\n--- trying undocumented enumeration / scene commands (errors are expected for some) ---")
    for line in ["prminfo 0", "prminfo 1", "mtrinfo 0", "mtrinfo 1",
                 "sscurrent_ex MIXER:Lib/Scene", "sscurrentt_ex MIXER:Lib/Scene",
                 "scpmode sstype \"text\""]:
        r.request(line)
    print("\nIf prminfo answers, run:  raw HOST \"prminfo N\"  for N = 0,1,2,... to list every parameter.")


def cmd_dump(a):
    r = Rcp(a.host, "dump", echo=False)
    r.setup_session()
    counts = dict(COUNTS)
    if a.compact:
        counts["InCh"] = 72
    snap = {}
    for group, paths in DUMP_PATHS.items():
        for x in range(counts[group]):
            for p in paths:
                addr = f"MIXER:Current/{group}/{p}"
                v = parse_value(r.request(f"get {addr} {x} 0", timeout=1.0, multi_quiet=0.05), addr)
                snap.setdefault(f"{group}/{p}", {})[x] = v
            print(f"\r{group} {x + 1}/{counts[group]}", end="", flush=True)
        print()
    out = f"dm7-snapshot-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1)
    errors = sum(1 for g in snap.values() for v in g.values() if isinstance(v, dict) or v is None)
    print(f"[ok] snapshot -> {out}  ({errors} gets failed or got no answer: see the log)")


def cmd_watch(a):
    r = Rcp(a.host, "watch")
    r.setup_session()
    r.on_notify = lambda line: print(f"{time.strftime('%H:%M:%S')}  {line}", flush=True)
    print(f"[watch] move faders, press keys, rename channels on the desk ({a.seconds} s, Ctrl+C to stop)")
    try:
        time.sleep(a.seconds)
    except KeyboardInterrupt:
        pass


def cmd_get(a):
    r = Rcp(a.host, "get"); r.setup_session()
    r.request(f"get {a.addr} {a.x} {a.y}")


def cmd_set(a):
    r = Rcp(a.host, "set"); r.setup_session()
    r.request(f"set {a.addr} {a.x} {a.y} {a.value}")


def cmd_raw(a):
    r = Rcp(a.host, "raw"); r.setup_session()
    r.request(a.line, timeout=4.0, multi_quiet=1.0)


def cmd_multicast(a):
    grp, port = "239.192.0.164", 50240
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(grp), socket.inet_aton(a.iface))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(1)
    print(f"[udp] joined {grp}:{port}, listening {a.seconds} s")
    end = time.time() + a.seconds
    while time.time() < end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        print(f"{addr[0]}:{addr[1]} {len(data)} bytes: {data[:200]!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info"); p.add_argument("host")
    p = sub.add_parser("dump"); p.add_argument("host"); p.add_argument("--compact", action="store_true")
    p = sub.add_parser("watch"); p.add_argument("host"); p.add_argument("--seconds", type=int, default=300)
    p = sub.add_parser("get"); p.add_argument("host"); p.add_argument("addr"); p.add_argument("x"); p.add_argument("y")
    p = sub.add_parser("set"); p.add_argument("host"); p.add_argument("addr"); p.add_argument("x"); p.add_argument("y"); p.add_argument("value")
    p = sub.add_parser("raw"); p.add_argument("host"); p.add_argument("line")
    p = sub.add_parser("multicast"); p.add_argument("--iface", default="0.0.0.0"); p.add_argument("--seconds", type=int, default=30)
    a = ap.parse_args()
    {"info": cmd_info, "dump": cmd_dump, "watch": cmd_watch, "get": cmd_get, "set": cmd_set,
     "raw": cmd_raw, "multicast": cmd_multicast}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
