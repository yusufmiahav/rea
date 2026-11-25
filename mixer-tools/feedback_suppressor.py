#!/usr/bin/env python3
"""Feedback suppressor for the Qu-16 and DM7: finds howling frequencies in a desk
feed and puts narrow cuts on that channel's own parametric EQ.

Needs Python 3.9+ and numpy. For live input also: pip install sounddevice

  python feedback_suppressor.py simulate                 # closed-loop demo, writes WAVs
  python feedback_suppressor.py analyse show.wav --channel 3
  python feedback_suppressor.py devices                  # list audio inputs
  python feedback_suppressor.py live --device "Qu-16" --channel 1 \\
         --desk qu --host 192.168.1.50 --target Ip1      # dry run: prints what it would send
  python feedback_suppressor.py live ... --apply         # really writes the EQ
  python feedback_suppressor.py notch --desk dm7 --host 192.168.0.128 --target InCh:0 \\
         --band 1 --freq 2500 --gain -6 --apply          # one manual notch, to check scaling
  python feedback_suppressor.py clear --desk qu --host ... --target Ip1 --bands 2,3 --apply

Without --apply nothing touches the desk.
"""
import argparse
import sys
import time

import numpy as np

from feedback.audio import live_stream, list_devices, read_wav, write_wav
from feedback.desks import QU_GEQ_FREQS, DryRun, Dm7Desk, QuDesk, QuGeqDesk, QuSplitDesk, load_calibration
from feedback.detector import SPEEDS, DetectorConfig, FeedbackDetector, speed_config
from feedback.notches import GeqManager, Notch, NotchConfig, NotchManager, SplitManager
from feedback.suppressor import Suppressor

DESK_DEFAULTS = {  # desk -> (default bands, deepest cut in dB)
    "qu": ([2, 3], -12.0),   # LM and HM; the Qu PEQ only goes to -12 dB
    "qu-geq": ([], -12.0),   # GEQ: nearest of 28 faders, mixes and LR only
    "qu-split": ([2, 3], -12.0),  # PEQ for severe howls + GEQ for gentle ones, mixes and LR only
    "dm7": ([2, 3], -18.0),
    "dry": ([2, 3], -12.0),
}


def make_desk(a):
    if a.desk == "qu":
        return QuDesk(a.host, a.target, midi_channel=a.midi_channel, apply=a.apply,
                      calibration=load_calibration(a.calibration))
    if a.desk == "qu-split":
        return QuSplitDesk(a.host, a.target, midi_channel=a.midi_channel, apply=a.apply)
    if a.desk == "qu-geq":
        return QuGeqDesk(a.host, a.target, midi_channel=a.midi_channel, apply=a.apply)
    if a.desk == "dm7":
        return Dm7Desk(a.host, a.target, apply=a.apply)
    return DryRun(a.target or "(none)")


def notch_cfg(a):
    bands, depth = DESK_DEFAULTS[a.desk]
    if getattr(a, "bands", None):
        bands = [int(b) for b in a.bands.split(",")]
    return NotchConfig(bands=bands, max_depth_db=max(depth, a.max_depth) if a.max_depth else depth,
                       start_depth_db=a.start_depth, width_oct=a.width,
                       hpf_max_hz=getattr(a, "hpf_max", 0.0) or 0.0,
                       cooldown_s=SPEEDS[getattr(a, "speed", "normal")]["cooldown_s"])


def det_cfg(a, sr):
    speed = getattr(a, "speed", "normal")
    if speed != "normal":
        return speed_config(speed, sr, a.papr, floor_db=getattr(a, "floor", -50.0))
    return DetectorConfig(sample_rate=sr, papr_db=a.papr, persist_s=a.persist,
                          floor_db=getattr(a, "floor", -50.0))


def cmd_simulate(a):
    from feedback import synth
    from feedback.simloop import SimDesk, run_loop
    sr = 48000
    src = synth.speechlike(a.seconds, sr=sr, level=0.05)
    write_wav("sim-no-control.wav", run_loop(src, sr, None, SimDesk(sr), howl_hz=a.freq, loop_gain_db=a.loop_gain) / 4, sr)
    desk = SimDesk(sr)
    clock = [0.0]
    sup = Suppressor(desk, DetectorConfig(sample_rate=sr), NotchConfig(bands=[1, 2, 3, 4]), clock=lambda: clock[0])

    class Timed:
        def process(self, b):
            clock[0] += len(b) / sr
            sup.process(b)

    mic = run_loop(src, sr, Timed(), desk, howl_hz=a.freq, loop_gain_db=a.loop_gain)
    write_wav("sim-with-suppressor.wav", mic / 4, sr)
    print("Notches placed:", sup.manager.to_json())
    print("Wrote sim-no-control.wav and sim-with-suppressor.wav (both scaled by 1/4).")


def cmd_analyse(a):
    x, sr = read_wav(a.file, a.channel - 1)
    d = FeedbackDetector(det_cfg(a, sr))
    for i in range(0, len(x), 1024):
        for ev in d.process(x[i:i + 1024]):
            print(f"{ev.time_s:8.2f} s  {ev.freq_hz:8.1f} Hz  {ev.level_db:6.1f} dBFS  "
                  f"PAPR {ev.papr_db:4.1f}  PNPR {ev.pnpr_db:4.1f}  PHPR {ev.phpr_db:4.1f}")
            d.forget(ev.freq_hz)


def cmd_devices(a):
    print(list_devices())


def cmd_live(a):
    desk = make_desk(a)
    cfg = notch_cfg(a)
    if a.desk != "qu-geq":
        desk.check_bands(cfg.bands)
    print("Desk link:", desk.check_link())
    manager = GeqManager(QU_GEQ_FREQS, cfg) if a.desk == "qu-geq" else None
    if a.desk == "qu-split":
        gcfg = NotchConfig(max_depth_db=cfg.max_depth_db, release_after_s=cfg.release_after_s)
        manager = SplitManager(NotchManager(cfg), GeqManager(QU_GEQ_FREQS, gcfg))
    sup = Suppressor(desk, det_cfg(a, a.rate), cfg, manager=manager)
    if a.load:
        with open(a.load, encoding="utf-8") as f:
            sup.manager.load_json(f.read())
        for n in sup.manager.notches.values():
            desk.set_notch(n)
    print(f"Listening on input channel {a.channel} -> {desk.name} {a.target}. "
          f"{'WRITING TO DESK' if a.apply else 'dry run (add --apply to write the EQ)'}. Ctrl+C to stop.")
    stream = live_stream(a.device, a.channel - 1, a.rate, 512, sup.process)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()
        with open(a.save, "w", encoding="utf-8") as f:
            f.write(sup.manager.to_json())
        print(f"\nSaved notches to {a.save}")
        desk.close()


def cmd_notch(a):
    desk = make_desk(a)
    desk.set_notch(Notch(band=a.band, freq_hz=a.freq, gain_db=a.gain, width_oct=a.width))
    time.sleep(0.5)
    desk.close()


def cmd_clear(a):
    desk = make_desk(a)
    for b in a.bands.split(","):
        desk.clear_notch(int(b))
    time.sleep(0.5)
    desk.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def desk_args(p):
        p.add_argument("--desk", choices=["dry", "qu", "qu-geq", "qu-split", "dm7"], default="dry")
        p.add_argument("--host", help="desk IP address")
        p.add_argument("--target", default="", help="Qu: Ip1, ST1, Mix1, LR. DM7: InCh:0, Mix:3 (0-based)")
        p.add_argument("--midi-channel", type=int, default=1, help="Qu MIDI channel (Setup > Control > MIDI)")
        p.add_argument("--calibration", help="Qu calibration JSON (see README)")
        p.add_argument("--apply", action="store_true", help="really write to the desk")

    def tuning(p):
        p.add_argument("--bands", help="PEQ bands to use, 1-4 as on the desk, e.g. 2,3 (Qu: 1=LF 2=LM 3=HM 4=HF)")
        p.add_argument("--start-depth", type=float, default=-6.0)
        p.add_argument("--max-depth", type=float, default=None)
        p.add_argument("--width", type=float, default=1 / 6, help="starting cut width in octaves (repeat rings widen it)")
        p.add_argument("--hpf-max", type=float, default=0.0,
                       help="raise the input's HPF for low howls, up to this many Hz (0 = off; inputs only)")
        p.add_argument("--papr", type=float, default=16.0, help="lower = more sensitive")
        p.add_argument("--persist", type=float, default=0.50, help="seconds a peak must hold")
        p.add_argument("--speed", choices=list(SPEEDS), default="normal",
                       help="fast / very fast act on howls sooner and more sensitively (overrides --persist)")
        p.add_argument("--floor", type=float, default=-50.0, help="ignore peaks quieter than this (dBFS)")

    p = sub.add_parser("simulate"); p.add_argument("--seconds", type=float, default=8)
    p.add_argument("--freq", type=float, default=2000.0); p.add_argument("--loop-gain", type=float, default=1.0)
    p = sub.add_parser("analyse"); p.add_argument("file"); p.add_argument("--channel", type=int, default=1)
    p.add_argument("--papr", type=float, default=16.0); p.add_argument("--persist", type=float, default=0.50)
    sub.add_parser("devices")
    p = sub.add_parser("live"); desk_args(p); tuning(p)
    p.add_argument("--device", help="audio input name or number (see `devices`)")
    p.add_argument("--channel", type=int, default=1, help="1-based input channel on that device")
    p.add_argument("--rate", type=int, default=48000)
    p.add_argument("--save", default="notches.json"); p.add_argument("--load")
    p = sub.add_parser("notch"); desk_args(p)
    p.add_argument("--band", type=int, required=True); p.add_argument("--freq", type=float, required=True)
    p.add_argument("--gain", type=float, default=-6.0); p.add_argument("--width", type=float, default=1 / 6)
    p = sub.add_parser("clear"); desk_args(p); p.add_argument("--bands", required=True)

    a = ap.parse_args()
    if getattr(a, "desk", "dry") in ("qu", "dm7") and a.apply and not a.host:
        ap.error("--apply needs --host")
    {"simulate": cmd_simulate, "analyse": cmd_analyse, "devices": cmd_devices, "live": cmd_live,
     "notch": cmd_notch, "clear": cmd_clear}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
