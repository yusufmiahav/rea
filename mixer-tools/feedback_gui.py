#!/usr/bin/env python3
"""Windowed front end for the feedback suppressor (this is what the .exe runs)."""
import json
import os
import queue
import sys
import threading
import time
try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:  # tool mode and self-test work without Tk
    tk = messagebox = ttk = None

import numpy as np

from feedback.desks import DryRun, Dm7Desk, QuDesk, load_calibration
from feedback.detector import DetectorConfig
from feedback.notches import NotchConfig
from feedback.suppressor import Suppressor

APP = "Mixer Tools"
STATE_DIR = os.path.join(os.path.expanduser("~"), "MixerTools")
SETTINGS = os.path.join(STATE_DIR, "settings.json")
NOTCHES = os.path.join(STATE_DIR, "notches.json")
DESKS = {"Dry run (no desk)": "dry", "Allen & Heath Qu-16": "qu", "Yamaha DM7": "dm7"}
MAX_DEPTH = {"dry": -12.0, "qu": -12.0, "dm7": -18.0}


class QueueWriter:
    """Sends print() output (the dry-run lines) into the log window."""

    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s.strip():
            self.q.put(("log", s.rstrip()))

    def flush(self):
        pass


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP)
        root.geometry("820x620")
        root.minsize(700, 520)
        self.q = queue.Queue()
        sys.stdout = QueueWriter(self.q)
        sys.stderr = QueueWriter(self.q)
        self.running = False
        self.stream = None
        self.sup = None
        self.desk = None
        self.blocks = queue.Queue(maxsize=200)
        self.peak_db = -120.0
        self.devices = []
        self._build()
        self._load_settings()
        self.refresh_devices()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._pump)

    # ---------------------------------------------------------------- UI
    def _build(self):
        pad = {"padx": 6, "pady": 4}
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        tab = ttk.Frame(nb)
        nb.add(tab, text="Feedback suppressor")
        tools = ttk.Frame(nb)
        nb.add(tools, text="Qu-16 / DM7 tools")
        self._build_tools(tools)
        top = ttk.Frame(tab)
        top.pack(fill="x", **pad)

        audio = ttk.LabelFrame(top, text="Audio input (a feed from the desk, taken after the EQ)")
        audio.pack(fill="x", **pad)
        self.dev_var = tk.StringVar()
        self.dev_box = ttk.Combobox(audio, textvariable=self.dev_var, state="readonly", width=60)
        self.dev_box.grid(row=0, column=0, columnspan=3, sticky="we", **pad)
        ttk.Button(audio, text="Refresh", command=self.refresh_devices).grid(row=0, column=3, **pad)
        ttk.Label(audio, text="Channel").grid(row=1, column=0, sticky="e", **pad)
        self.chan_var = tk.IntVar(value=1)
        ttk.Spinbox(audio, from_=1, to=64, textvariable=self.chan_var, width=5).grid(row=1, column=1, sticky="w", **pad)
        self.meter = ttk.Progressbar(audio, maximum=60, length=260)
        self.meter.grid(row=1, column=2, sticky="we", **pad)
        self.meter_lbl = ttk.Label(audio, text="-- dBFS", width=10)
        self.meter_lbl.grid(row=1, column=3, **pad)
        audio.columnconfigure(2, weight=1)

        desk = ttk.LabelFrame(top, text="Desk")
        desk.pack(fill="x", **pad)
        self.desk_var = tk.StringVar(value=list(DESKS)[0])
        ttk.Combobox(desk, textvariable=self.desk_var, values=list(DESKS), state="readonly", width=22).grid(row=0, column=0, **pad)
        ttk.Label(desk, text="IP").grid(row=0, column=1, sticky="e")
        self.host_var = tk.StringVar()
        ttk.Entry(desk, textvariable=self.host_var, width=16).grid(row=0, column=2, **pad)
        ttk.Label(desk, text="Target").grid(row=0, column=3, sticky="e")
        self.target_var = tk.StringVar(value="Ip1")
        ttk.Entry(desk, textvariable=self.target_var, width=10).grid(row=0, column=4, **pad)
        ttk.Label(desk, text="EQ bands").grid(row=0, column=5, sticky="e")
        self.bands_var = tk.StringVar(value="1,2")
        ttk.Entry(desk, textvariable=self.bands_var, width=8).grid(row=0, column=6, **pad)
        ttk.Label(desk, text="Qu MIDI ch").grid(row=1, column=1, sticky="e")
        self.midi_var = tk.IntVar(value=1)
        ttk.Spinbox(desk, from_=1, to=16, textvariable=self.midi_var, width=5).grid(row=1, column=2, sticky="w", **pad)
        ttk.Label(desk, text="Target: Qu Ip1 / ST1 / Mix1 / LR    DM7 InCh:0 / Mix:3 (0-based)",
                  foreground="#666").grid(row=1, column=3, columnspan=4, sticky="w", **pad)
        self.apply_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(desk, text="Write to the desk (off = only show what it would send)",
                        variable=self.apply_var).grid(row=2, column=0, columnspan=7, sticky="w", **pad)

        tune = ttk.LabelFrame(top, text="Detection")
        tune.pack(fill="x", **pad)
        ttk.Label(tune, text="Sensitivity").grid(row=0, column=0, **pad)
        self.sens_var = tk.DoubleVar(value=16.0)
        ttk.Scale(tune, from_=24, to=10, variable=self.sens_var, length=220).grid(row=0, column=1, **pad)
        ttk.Label(tune, text="(left = only loud, obvious howls; right = catches them earlier)",
                  foreground="#666").grid(row=0, column=2, sticky="w", **pad)

        btns = ttk.Frame(tab)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="Start", command=self.toggle)
        self.start_btn.pack(side="left", **pad)
        self.freeze_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Freeze (keep listening, change nothing)", variable=self.freeze_var,
                        command=self._apply_freeze).pack(side="left", **pad)
        ttk.Button(btns, text="Clear notches", command=self.clear).pack(side="left", **pad)
        ttk.Button(btns, text="Run simulation", command=self.simulate).pack(side="right", **pad)

        mid = ttk.Panedwindow(tab, orient="horizontal")
        mid.pack(fill="both", expand=True, **pad)
        nf = ttk.LabelFrame(mid, text="Notches on the desk")
        self.notch_list = tk.Listbox(nf, width=34, font=("Consolas", 10))
        self.notch_list.pack(fill="both", expand=True)
        mid.add(nf, weight=1)
        lf = ttk.LabelFrame(mid, text="Log")
        self.log_txt = tk.Text(lf, height=12, wrap="none", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(lf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_txt.pack(fill="both", expand=True)
        mid.add(lf, weight=3)

        self.status = ttk.Label(tab, text="Stopped", anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0, 6))

    def log(self, text):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def _show_notches(self):
        self.notch_list.delete(0, "end")
        if not self.sup:
            return
        for n in sorted(self.sup.manager.notches.values(), key=lambda n: n.band):
            self.notch_list.insert("end", f"band {n.band}  {n.freq_hz:8.1f} Hz  {n.gain_db:+5.1f} dB")

    # ------------------------------------------------------------ devices
    def refresh_devices(self):
        try:
            import sounddevice as sd
            self.devices = [(i, d) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
            hostapis = sd.query_hostapis()
            names = [f"{i}: {d['name']}  [{hostapis[d['hostapi']]['name']}, {d['max_input_channels']} in]"
                     for i, d in self.devices]
        except Exception as e:  # no audio stack
            names = []
            self.log(f"Could not list audio devices: {e}")
        self.dev_box["values"] = names
        saved = self.dev_var.get()
        if saved not in names and names:
            self.dev_var.set(names[0])

    # ---------------------------------------------------------- run/stop
    def _make_desk(self, kind, apply):
        host, target = self.host_var.get().strip(), self.target_var.get().strip()
        if kind == "qu":
            cal = os.path.join(STATE_DIR, "qu-calibration.json")
            return QuDesk(host, target, midi_channel=self.midi_var.get(), apply=apply,
                          calibration=load_calibration(cal) if os.path.exists(cal) else None)
        if kind == "dm7":
            return Dm7Desk(host, target, apply=apply)
        return DryRun(target or "(none)")

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        kind = DESKS[self.desk_var.get()]
        apply = self.apply_var.get() and kind != "dry"
        if apply and not self.host_var.get().strip():
            messagebox.showerror(APP, "Enter the desk's IP address first.")
            return
        if apply and not messagebox.askyesno(APP, "This will change the EQ on the desk. Carry on?"):
            return
        if not self.dev_var.get():
            messagebox.showerror(APP, "No audio input selected.")
            return
        try:
            bands = [int(b) for b in self.bands_var.get().split(",") if b.strip()]
            self.desk = self._make_desk(kind, apply)
            dev_index = int(self.dev_var.get().split(":", 1)[0])
            info = dict(self.devices)[dev_index]
            sr = int(info["default_samplerate"]) or 48000
            chan = self.chan_var.get() - 1
            if chan >= info["max_input_channels"]:
                raise ValueError(f"that device has {info['max_input_channels']} input channels")
            self.sup = Suppressor(self.desk, DetectorConfig(sample_rate=sr, papr_db=self.sens_var.get()),
                                  NotchConfig(bands=bands, max_depth_db=MAX_DEPTH[kind]),
                                  log=lambda m: self.q.put(("log", m)))
            self.sup.frozen = self.freeze_var.get()
            if os.path.exists(NOTCHES) and messagebox.askyesno(APP, "Put back the notches from last time?"):
                with open(NOTCHES, encoding="utf-8") as f:
                    self.sup.manager.load_json(f.read())
                for n in self.sup.manager.notches.values():
                    self.desk.set_notch(n)

            import sounddevice as sd

            def cb(indata, frames, t, status):
                x = indata[:, chan].astype(np.float64)
                self.peak_db = 20 * np.log10(np.abs(x).max() + 1e-9)
                try:
                    self.blocks.put_nowait(x)
                except queue.Full:
                    pass

            self.stream = sd.InputStream(device=dev_index, channels=chan + 1, samplerate=sr,
                                         blocksize=512, dtype="float32", callback=cb)
            self.running = True
            threading.Thread(target=self._worker, daemon=True).start()
            self.stream.start()
        except Exception as e:
            self.running = False
            if self.desk:
                self.desk.close()
            messagebox.showerror(APP, f"Could not start: {e}")
            return
        self.start_btn.configure(text="Stop")
        self.status.configure(text=f"Listening at {sr} Hz -> {self.desk_var.get()} {self.target_var.get()}"
                                   f"{'  (WRITING TO DESK)' if apply else '  (dry run)'}")
        self.log("Started")
        self._save_settings()

    def _worker(self):
        while self.running:
            try:
                x = self.blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.sup.process(x)
            except Exception as e:
                self.q.put(("log", f"error: {e}"))
            self.q.put(("notches", None))

    def stop(self):
        self.running = False
        if self.stream:
            self.stream.close()
            self.stream = None
        if self.sup:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(NOTCHES, "w", encoding="utf-8") as f:
                f.write(self.sup.manager.to_json())
            self.log(f"Saved notches to {NOTCHES}")
        if self.desk:
            self.desk.close()
            self.desk = None
        self.start_btn.configure(text="Start")
        self.status.configure(text="Stopped")

    def clear(self):
        if self.sup and self.desk:
            self.sup.clear()
            self.log("Cleared all notches")
            self._show_notches()
        else:
            self.log("Start first, then clear (clearing talks to the desk)")

    def _apply_freeze(self):
        if self.sup:
            self.sup.frozen = self.freeze_var.get()

    def simulate(self):
        self.log("Simulating a room that howls at 2 kHz, with speech, for 8 s...")

        def run():
            from feedback import synth
            from feedback.simloop import SimDesk, run_loop
            sr = 48000
            src = synth.speechlike(8, sr=sr, level=0.05)
            raw = run_loop(src, sr, None, SimDesk(sr))
            desk = SimDesk(sr)
            clock = [0.0]
            sup = Suppressor(desk, DetectorConfig(sample_rate=sr), NotchConfig(bands=[0, 1, 2, 3]),
                             clock=lambda: clock[0], log=lambda m: self.q.put(("log", m)))

            class Timed:
                def process(self, b):
                    clock[0] += len(b) / sr
                    sup.process(b)

            mic = run_loop(src, sr, Timed(), desk)
            db = lambda x: 20 * np.log10(np.sqrt(np.mean(x[-2 * sr:] ** 2)) + 1e-12)
            self.q.put(("log", f"Without suppressor: {db(raw) - db(src):+.1f} dB over the voice (howling)"))
            self.q.put(("log", f"With suppressor:    {db(mic) - db(src):+.1f} dB over the voice"))

        threading.Thread(target=run, daemon=True).start()


    # ------------------------------------------------------- desk tools
    TOOL_CMDS = [
        ("Qu-16: find desks on the network", "qu16 discover"),
        ("Qu-16: full state + live changes (60 s)", "qu16 sync {host} --seconds 60"),
        ("Qu-16: input meters (20 s)", "qu16 meters {host} --seconds 20"),
        ("Qu-16: listen on private port 51326 (15 s)", "qu16 listen-private {host} --seconds 15"),
        ("DM7: device info", "dm7 info {host}"),
        ("DM7: snapshot of main parameters", "dm7 dump {host}"),
        ("DM7: watch changes made on the desk (300 s)", "dm7 watch {host} --seconds 300"),
        ("DM7: get one parameter", "dm7 get {host} MIXER:Current/InCh/PEQ/Band/Gain 0 1"),
        ("DM7: set one parameter (CHANGES THE DESK)", "dm7 set {host} MIXER:Current/InCh/Fader/Level 0 0 -32768"),
        ("DM7: raw RCP line", 'dm7 raw {host} "devinfo productname"'),
        ("DM7: listen for discovery multicast (30 s)", "dm7 multicast --seconds 30"),
        ("Feedback: one manual test notch (dry run)", "feedback notch --desk qu --target Ip1 --band 2 --freq 2500 --gain -6"),
    ]

    def _build_tools(self, tab):
        pad = {"padx": 6, "pady": 4}
        f = ttk.Frame(tab)
        f.pack(fill="x", **pad)
        ttk.Label(f, text="Desk IP").grid(row=0, column=0, sticky="e", **pad)
        self.tool_host = tk.StringVar()
        ttk.Entry(f, textvariable=self.tool_host, width=18).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="Tool").grid(row=1, column=0, sticky="e", **pad)
        self.tool_pick = tk.StringVar(value=self.TOOL_CMDS[0][0])
        box = ttk.Combobox(f, textvariable=self.tool_pick, values=[t[0] for t in self.TOOL_CMDS],
                           state="readonly", width=55)
        box.grid(row=1, column=1, columnspan=3, sticky="w", **pad)
        box.bind("<<ComboboxSelected>>", lambda e: self._fill_tool())
        ttk.Label(f, text="Command").grid(row=2, column=0, sticky="e", **pad)
        self.tool_cmd = tk.StringVar()
        ttk.Entry(f, textvariable=self.tool_cmd, width=80).grid(row=2, column=1, columnspan=3, sticky="we", **pad)
        f.columnconfigure(3, weight=1)
        b = ttk.Frame(tab)
        b.pack(fill="x", **pad)
        self.tool_btn = ttk.Button(b, text="Run", command=self.run_tool)
        self.tool_btn.pack(side="left", **pad)
        ttk.Button(b, text="Stop", command=self.stop_tool).pack(side="left", **pad)
        ttk.Button(b, text="Open logs folder", command=lambda: os.startfile(self._logs_dir())
                   if hasattr(os, "startfile") else None).pack(side="left", **pad)
        ttk.Label(b, text="Raw logs from each run are saved in the logs folder.", foreground="#666").pack(side="left", **pad)
        self.tool_out = tk.Text(tab, wrap="none", font=("Consolas", 9), state="disabled")
        self.tool_out.pack(fill="both", expand=True, **pad)
        self.tool_proc = None
        self.tool_file = None
        self._fill_tool()

    def _logs_dir(self):
        d = os.path.join(STATE_DIR, "logs")
        os.makedirs(d, exist_ok=True)
        return d

    def _fill_tool(self):
        tmpl = dict(self.TOOL_CMDS)[self.tool_pick.get()]
        self.tool_cmd.set(tmpl.replace("{host}", self.tool_host.get().strip() or self.host_var.get().strip() or "<desk-ip>"))

    def _tool_print(self, text):
        self.tool_out.configure(state="normal")
        self.tool_out.insert("end", text)
        self.tool_out.see("end")
        self.tool_out.configure(state="disabled")

    def run_tool(self):
        import shlex
        import subprocess
        if self.tool_proc and self.tool_proc.poll() is None:
            messagebox.showinfo(APP, "A tool is already running. Stop it first.")
            return
        args = shlex.split(self.tool_cmd.get(), posix=True)
        if any("<desk-ip>" in a for a in args):
            messagebox.showerror(APP, "Enter the desk's IP address first.")
            return
        if args[:2] == ["dm7", "set"] and not messagebox.askyesno(APP, "This changes a setting on the desk. Carry on?"):
            return
        out = os.path.join(self._logs_dir(), f"run-{time.strftime('%Y%m%d-%H%M%S')}.txt")
        env = dict(os.environ, FS_TOOL_OUT=out)
        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--tool", *args]
        else:
            cmd = [sys.executable, os.path.abspath(__file__), "--tool", *args]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        open(out, "w").close()
        self.tool_proc = subprocess.Popen(cmd, cwd=self._logs_dir(), env=env, creationflags=flags,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.tool_file = open(out, encoding="utf-8", errors="replace")
        self.tool_out.configure(state="normal"); self.tool_out.delete("1.0", "end"); self.tool_out.configure(state="disabled")
        self._tool_print("$ " + " ".join(args) + "\n")
        self.root.after(200, self._tail_tool)

    def _tail_tool(self):
        if not self.tool_file:
            return
        chunk = self.tool_file.read()
        if chunk:
            self._tool_print(chunk.replace("\r", "\n"))
        if self.tool_proc.poll() is None:
            self.root.after(200, self._tail_tool)
        else:
            rest = self.tool_file.read()
            if rest:
                self._tool_print(rest.replace("\r", "\n"))
            self._tool_print(f"\n[finished, exit code {self.tool_proc.returncode}]\n")
            self.tool_file.close()
            self.tool_file = None

    def stop_tool(self):
        if self.tool_proc and self.tool_proc.poll() is None:
            self.tool_proc.terminate()
            self._tool_print("\n[stopped]\n")

    # ------------------------------------------------------------- misc
    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self.log(val)
                elif kind == "notches":
                    pass
        except queue.Empty:
            pass
        if self.running:
            self._show_notches()
            v = max(-60.0, self.peak_db)
            self.meter["value"] = v + 60
            self.meter_lbl.configure(text=f"{v:5.1f} dBFS")
        self.root.after(100, self._pump)

    def _save_settings(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        data = {k: getattr(self, k + "_var").get() for k in
                ("dev", "chan", "desk", "host", "target", "bands", "midi", "sens")}
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _load_settings(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                getattr(self, k + "_var").set(v)
        except (OSError, ValueError, AttributeError):
            pass

    def on_close(self):
        if self.running:
            self.stop()
        self.root.destroy()


def selftest():
    """Used by the build to check the packaged exe works without opening a window."""
    from feedback import synth
    from feedback.detector import FeedbackDetector
    import sounddevice  # noqa: F401  (must be bundled)
    d = FeedbackDetector()
    x = synth.speechlike(4) + synth.howl(4, 2500.0)
    evs = []
    for i in range(0, len(x), 512):
        evs += d.process(x[i:i + 512])
    ok = bool(evs) and abs(evs[0].freq_hz - 2500) < 5
    with open(os.environ.get("SELFTEST_OUT", "selftest.txt"), "w") as f:
        f.write("OK" if ok else "FAIL")
    return 0 if ok else 1


def run_tool_mode(args):
    """`--tool qu16|dm7|feedback ...`: run one of the command-line tools. Output goes to
    the file named in FS_TOOL_OUT (the window tails it), or to the console."""
    out = os.environ.get("FS_TOOL_OUT")
    if out:
        sys.stdout = sys.stderr = open(out, "a", buffering=1, encoding="utf-8")
    name, rest = args[0], args[1:]
    if name == "qu16":
        from desktools import qu16_probe as mod
    elif name == "dm7":
        from desktools import dm7_rcp_probe as mod
    elif name == "feedback":
        import feedback_suppressor as mod
    else:
        print(f"unknown tool {name!r}; use qu16, dm7 or feedback")
        return 2
    sys.argv = [name, *rest]
    try:
        return mod.main() or 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0
    except KeyboardInterrupt:
        return 0


def main():
    if "--tool" in sys.argv:
        i = sys.argv.index("--tool")
        sys.exit(run_tool_mode(sys.argv[i + 1:]))
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--smoke-gui" in sys.argv:  # build check: open the window, then close it
        root = tk.Tk()
        App(root)
        def done():
            with open(os.environ.get("SELFTEST_OUT", "smoke.txt"), "w") as f:
                f.write("OK")
            root.destroy()
        root.after(2500, done)
        root.mainloop()
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
