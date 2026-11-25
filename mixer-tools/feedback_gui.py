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

from feedback.desks import QU_BAND_NAMES, QU_GEQ_FREQS, DryRun, Dm7Desk, QuDesk, QuGeqDesk, load_calibration
from feedback.detector import DetectorConfig
from feedback.notches import GeqManager, NotchConfig
from feedback.suppressor import Suppressor

APP = "Mixer Tools"
STATE_DIR = os.path.join(os.path.expanduser("~"), "MixerTools")
SETTINGS = os.path.join(STATE_DIR, "settings.json")
NOTCHES = os.path.join(STATE_DIR, "notches.json")
DESKS = {"Dry run (no desk)": "dry", "Qu-16 PEQ (any channel or mix)": "qu",
         "Qu-16 GEQ (mixes and LR only)": "qu-geq", "Yamaha DM7 PEQ": "dm7"}
MAX_DEPTH = {"dry": -12.0, "qu": -12.0, "qu-geq": -12.0, "dm7": -18.0}


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
        root.geometry("1000x800")
        root.minsize(860, 680)
        self.q = queue.Queue()
        sys.stdout = QueueWriter(self.q)
        sys.stderr = QueueWriter(self.q)
        self.running = False
        self.stream = None
        self.desk = None
        self.blocks = queue.Queue(maxsize=200)
        self.devices = []
        self.mode = "dry"
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

        audio = ttk.LabelFrame(top, text="Audio input (feeds from the desk, taken after the EQ)")
        audio.pack(fill="x", **pad)
        self.dev_var = tk.StringVar()
        self.dev_box = ttk.Combobox(audio, textvariable=self.dev_var, state="readonly", width=70)
        self.dev_box.grid(row=0, column=0, sticky="we", **pad)
        ttk.Button(audio, text="Refresh", command=self.refresh_devices).grid(row=0, column=1, **pad)
        audio.columnconfigure(0, weight=1)

        desk = ttk.LabelFrame(top, text="Desk")
        desk.pack(fill="x", **pad)
        self.desk_var = tk.StringVar(value=list(DESKS)[0])
        ttk.Combobox(desk, textvariable=self.desk_var, values=list(DESKS), state="readonly", width=30).grid(row=0, column=0, **pad)
        ttk.Label(desk, text="IP").grid(row=0, column=1, sticky="e")
        self.host_var = tk.StringVar()
        ttk.Entry(desk, textvariable=self.host_var, width=16).grid(row=0, column=2, **pad)
        ttk.Label(desk, text="Qu MIDI ch").grid(row=0, column=3, sticky="e")
        self.midi_var = tk.IntVar(value=1)
        ttk.Spinbox(desk, from_=1, to=16, textvariable=self.midi_var, width=5).grid(row=0, column=4, sticky="w", **pad)
        self.apply_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(desk, text="Write to the desk (off = only show what it would send)",
                        variable=self.apply_var).grid(row=1, column=0, columnspan=5, sticky="w", **pad)

        rows = ttk.LabelFrame(top, text="Channels to watch (each input channel corrects its own desk target)")
        rows.pack(fill="x", **pad)
        ed = ttk.Frame(rows)
        ed.pack(fill="x")
        ttk.Label(ed, text="Input ch").pack(side="left", padx=(6, 2))
        self.chan_var = tk.IntVar(value=1)
        ttk.Spinbox(ed, from_=1, to=64, textvariable=self.chan_var, width=4).pack(side="left")
        ttk.Label(ed, text="Target").pack(side="left", padx=(10, 2))
        self.target_var = tk.StringVar(value="Ip1")
        ttk.Entry(ed, textvariable=self.target_var, width=9).pack(side="left")
        ttk.Label(ed, text="PEQ bands").pack(side="left", padx=(10, 2))
        self.bands_var = tk.StringVar(value="2,3")
        ttk.Entry(ed, textvariable=self.bands_var, width=8).pack(side="left")
        ttk.Button(ed, text="Add / update", command=self.add_row).pack(side="left", padx=6)
        ttk.Button(ed, text="Remove", command=self.remove_row).pack(side="left")
        ttk.Label(rows, text="Target: Qu Ip1 / ST1 / Mix1 / LR, DM7 InCh:0 / Mix:3 (0-based).  "
                             "PEQ bands 1-4 as on the desk: 1=LF 2=LM 3=HM 4=HF (GEQ mode ignores bands).",
                  foreground="#666").pack(fill="x", padx=6)
        self.rows_tv = ttk.Treeview(rows, columns=("chan", "target", "bands", "level", "cuts"),
                                    show="headings", height=4)
        for col, title, w in (("chan", "Input ch", 70), ("target", "Target", 90), ("bands", "PEQ bands", 90),
                              ("level", "Level", 80), ("cuts", "Cuts on the desk", 420)):
            self.rows_tv.heading(col, text=title)
            self.rows_tv.column(col, width=w, anchor="w")
        self.rows_tv.pack(fill="x", padx=6, pady=4)
        self.rows_tv.bind("<<TreeviewSelect>>", lambda e: self._edit_selected())
        self.rows = []  # [{"chan": 1, "target": "Ip1", "bands": "2,3"}]

        tune = ttk.LabelFrame(top, text="Detection")
        tune.pack(fill="x", **pad)
        ttk.Label(tune, text="Sensitivity").grid(row=0, column=0, **pad)
        self.sens_var = tk.DoubleVar(value=16.0)
        ttk.Scale(tune, from_=24, to=10, variable=self.sens_var, length=200).grid(row=0, column=1, **pad)
        ttk.Label(tune, text="(left = only obvious howls; right = earlier)", foreground="#666").grid(row=0, column=2, sticky="w")
        ttk.Label(tune, text="Ignore below (dBFS)").grid(row=0, column=3, **pad)
        self.floor_var = tk.IntVar(value=-50)
        ttk.Spinbox(tune, from_=-90, to=-10, increment=5, textvariable=self.floor_var, width=6).grid(row=0, column=4, sticky="w", **pad)

        btns = ttk.Frame(tab)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="Start", command=self.toggle)
        self.start_btn.pack(side="left", **pad)
        self.freeze_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btns, text="Freeze (keep listening, change nothing)", variable=self.freeze_var,
                        command=self._apply_freeze).pack(side="left", **pad)
        ttk.Button(btns, text="Clear notches", command=self.clear).pack(side="left", **pad)
        ttk.Button(btns, text="Run simulation", command=self.simulate).pack(side="right", **pad)

        ro = ttk.LabelFrame(tab, text="Ring out: raise a fader slowly and cut each frequency that rings (press Start first)")
        ro.pack(fill="x", **pad)
        ttk.Label(ro, text="Fader of").grid(row=0, column=0, sticky="e", **pad)
        self.ro_target = tk.StringVar()
        self.ro_box = ttk.Combobox(ro, textvariable=self.ro_target, state="readonly", width=10)
        self.ro_box.grid(row=0, column=1, sticky="w", **pad)
        self.ro_vars = {}
        for i, (key, label, default) in enumerate((("start_db", "Start dB", -30.0), ("max_db", "Max dB", 0.0),
                                                   ("step_db", "Step dB", 1.0), ("step_s", "Every s", 2.0),
                                                   ("backoff_db", "Back off dB", 4.0), ("guard_dbfs", "Stop above dBFS", -3.0))):
            ttk.Label(ro, text=label).grid(row=0, column=2 + 2 * i, sticky="e", padx=(8, 2))
            v = tk.DoubleVar(value=default)
            ttk.Entry(ro, textvariable=v, width=6).grid(row=0, column=3 + 2 * i, sticky="w")
            self.ro_vars[key] = v
        self.ro_btn = ttk.Button(ro, text="Start ring-out", command=self.toggle_ringout)
        self.ro_btn.grid(row=1, column=0, columnspan=2, sticky="w", **pad)
        self.ro_status = ttk.Label(ro, text="Idle. The fader never goes above Max, backs off on every howl, "
                                            "and Stop puts it back to the start level.", foreground="#666")
        self.ro_status.grid(row=1, column=2, columnspan=12, sticky="w", **pad)
        self.ringout = None
        self.ro_row = None

        lf = ttk.LabelFrame(tab, text="Log")
        lf.pack(fill="both", expand=True, **pad)
        self.log_txt = tk.Text(lf, height=8, wrap="none", font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(lf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_txt.pack(fill="both", expand=True)

        self.status = ttk.Label(tab, text="Stopped", anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0, 6))

    def log(self, text):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", time.strftime("%H:%M:%S  ") + text + "\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    # ---------------------------------------------------------------- rows
    def add_row(self):
        chan, target, bands = self.chan_var.get(), self.target_var.get().strip(), self.bands_var.get().replace(" ", "")
        if not target:
            return
        if self.running:
            messagebox.showinfo(APP, "Stop first, then change the channel list.")
            return
        self.rows = [r for r in self.rows if r["target"] != target]
        self.rows.append({"chan": chan, "target": target, "bands": bands})
        self.rows.sort(key=lambda r: r["chan"])
        self._refresh_rows()

    def remove_row(self):
        if self.running:
            messagebox.showinfo(APP, "Stop first, then change the channel list.")
            return
        sel = self.rows_tv.selection()
        if sel:
            del self.rows[self.rows_tv.index(sel[0])]
            self._refresh_rows()

    def _edit_selected(self):
        sel = self.rows_tv.selection()
        if sel:
            r = self.rows[self.rows_tv.index(sel[0])]
            self.chan_var.set(r["chan"]); self.target_var.set(r["target"]); self.bands_var.set(r["bands"])

    def _refresh_rows(self):
        self.rows_tv.delete(*self.rows_tv.get_children())
        for r in self.rows:
            self.rows_tv.insert("", "end", values=(r["chan"], r["target"], r["bands"], r.get("level_txt", ""),
                                                   r.get("cuts_txt", "")))
        self.ro_box["values"] = [r["target"] for r in self.rows]
        if self.rows and self.ro_target.get() not in self.ro_box["values"]:
            self.ro_target.set(self.rows[0]["target"])

    def _cuts_text(self, sup):
        out = []
        for n in sorted(sup.manager.notches.values(), key=lambda n: n.band):
            if self.mode == "qu-geq":
                out.append(f"{n.freq_hz:g}Hz {n.gain_db:+.0f}")
            else:
                name = QU_BAND_NAMES.get(n.band, n.band) if self.mode == "qu" else f"B{n.band}"
                out.append(f"{name} {n.freq_hz:.0f}Hz {n.gain_db:+.0f}")
        return ", ".join(out)

    def _show_notches(self):
        for item, r in zip(self.rows_tv.get_children(), self.rows):
            if "sup" not in r:
                continue
            r["level_txt"] = f"{max(-90.0, r.get('peak', -120.0)):5.1f} dBFS"
            r["cuts_txt"] = self._cuts_text(r["sup"])
            self.rows_tv.item(item, values=(r["chan"], r["target"], r["bands"], r["level_txt"], r["cuts_txt"]))
        if self.ringout:
            state = "running" if self.ringout.running else "finished"
            txt = f"{self.ro_row['target']} fader {self.ringout.level:+.1f} dB ({state})"
            if self.ringout.result:
                txt = self.ringout.result
            self.ro_status.configure(text=txt, foreground="#000")
            if not self.ringout.running:
                self.ro_btn.configure(text="Start ring-out")

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
    def _make_desk(self, kind, apply, target):
        host = self.host_var.get().strip()
        log = lambda m: self.q.put(("log", m))
        if kind == "qu-geq":
            return QuGeqDesk(host, target, midi_channel=self.midi_var.get(), apply=apply, log=log)
        if kind == "qu":
            cal = os.path.join(STATE_DIR, "qu-calibration.json")
            return QuDesk(host, target, midi_channel=self.midi_var.get(), apply=apply, log=log,
                          calibration=load_calibration(cal) if os.path.exists(cal) else None)
        if kind == "dm7":
            return Dm7Desk(host, target, apply=apply, log=log)
        return DryRun(target or "(none)", log=log)

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.start()

    def start(self):
        kind = DESKS[self.desk_var.get()]
        apply = self.apply_var.get() and kind != "dry"
        if not self.rows:
            self.add_row()  # use what's typed in the editor
        if not self.rows:
            messagebox.showerror(APP, "Add at least one channel to watch.")
            return
        if apply and not self.host_var.get().strip():
            messagebox.showerror(APP, "Enter the desk's IP address first.")
            return
        if apply and not messagebox.askyesno(APP, f"This will change the EQ on {len(self.rows)} desk channel(s). Carry on?"):
            return
        if not self.dev_var.get():
            messagebox.showerror(APP, "No audio input selected.")
            return
        self.desk = None
        try:
            self.mode = kind
            for r in self.rows:
                r["band_list"] = [int(b) for b in r["bands"].split(",") if b]
                if kind in ("qu", "dm7"):
                    (QuDesk if kind == "qu" else Dm7Desk).check_bands(r["band_list"])
            dev_index = int(self.dev_var.get().split(":", 1)[0])
            info = dict(self.devices)[dev_index]
            sr = int(info["default_samplerate"]) or 48000
            nch = max(r["chan"] for r in self.rows)
            if nch > info["max_input_channels"]:
                raise ValueError(f"that audio device has {info['max_input_channels']} input channels; "
                                 f"input ch {nch} doesn't exist")
            # One connection to the desk (the Qu allows only one), one view per target.
            self.desk = self._make_desk(kind, apply, self.rows[0]["target"])
            self.log(f"Desk link OK: {self.desk.check_link()}")
            saved = self._read_saved_notches(kind)
            for r in self.rows:
                r["desk"] = self.desk.view(r["target"])
                ncfg = NotchConfig(bands=r["band_list"], max_depth_db=MAX_DEPTH[kind])
                manager = GeqManager(QU_GEQ_FREQS, ncfg) if kind == "qu-geq" else None
                r["sup"] = Suppressor(r["desk"], DetectorConfig(sample_rate=sr, papr_db=self.sens_var.get(),
                                                                floor_db=float(self.floor_var.get())),
                                      ncfg, log=lambda m, t=r["target"]: self.q.put(("log", f"{t}: {m}")),
                                      manager=manager, on_event=lambda a, e, row=r: self._on_howl(row, a, e))
                r["sup"].frozen = self.freeze_var.get()
                r["peak"] = -120.0
            if saved and messagebox.askyesno(APP, "Put back the cuts from last time?"):
                for r in self.rows:
                    if saved.get(r["target"]):
                        r["sup"].manager.load_json(json.dumps(saved[r["target"]]))
                        for n in r["sup"].manager.notches.values():
                            r["desk"].set_notch(n)

            import sounddevice as sd

            def cb(indata, frames, t, status):
                try:
                    self.blocks.put_nowait(indata.astype(np.float64))
                except queue.Full:
                    pass

            self.stream = sd.InputStream(device=dev_index, channels=nch, samplerate=sr,
                                         blocksize=512, dtype="float32", callback=cb)
            self.running = True
            threading.Thread(target=self._worker, daemon=True).start()
            self.stream.start()
        except Exception as e:
            self.running = False
            if self.desk:
                self.desk.close()
                self.desk = None
            messagebox.showerror(APP, f"Could not start: {e}")
            return
        self.start_btn.configure(text="Stop")
        self.status.configure(text=f"Listening at {sr} Hz on {len(self.rows)} channel(s) -> {self.desk_var.get()}"
                                   f"{'  (WRITING TO DESK)' if apply else '  (dry run)'}")
        self.log("Started: " + ", ".join(f"in {r['chan']} -> {r['target']}" for r in self.rows))
        self._save_settings()

    def _read_saved_notches(self, kind):
        try:
            with open(NOTCHES, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        # Older files used other band numbers or held one channel only: ignore them.
        if not isinstance(data, dict) or data.get("version") != 3 or data.get("mode") != kind:
            return None
        return data.get("targets") or None

    def _on_howl(self, row, action, ev):
        if self.ringout and self.ro_row is row:
            self.ringout.on_howl(action, ev.freq_hz)

    def _worker(self):
        while self.running:
            try:
                x = self.blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            for r in self.rows:
                try:
                    col = x[:, r["chan"] - 1]
                    r["peak"] = float(20 * np.log10(np.abs(col).max() + 1e-9))
                    r["sup"].process(col)
                    if self.ringout and self.ro_row is r:
                        self.ringout.tick(r["peak"])
                except Exception as e:
                    self.q.put(("log", f"{r['target']}: error: {type(e).__name__}: {e}"))

    def stop(self):
        if self.ringout and self.ringout.running:
            self.ringout.stop()
        self.running = False
        if self.stream:
            self.stream.close()
            self.stream = None
        if any("sup" in r for r in self.rows):
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(NOTCHES, "w", encoding="utf-8") as f:
                json.dump({"version": 3, "mode": self.mode,
                           "targets": {r["target"]: json.loads(r["sup"].manager.to_json())
                                       for r in self.rows if "sup" in r}}, f, indent=2)
            self.log(f"Saved cuts to {NOTCHES}")
        if self.desk:
            self.desk.close()
            self.desk = None
        self.start_btn.configure(text="Start")
        self.status.configure(text="Stopped")

    def clear(self):
        if not self.running:
            self.log("Start first, then clear (clearing talks to the desk)")
            return
        for r in self.rows:
            r["sup"].clear()
        self.log("Cleared all cuts")
        self._show_notches()

    def _apply_freeze(self):
        for r in self.rows:
            if "sup" in r:
                r["sup"].frozen = self.freeze_var.get()

    # ------------------------------------------------------------ ring-out
    def toggle_ringout(self):
        from feedback.ringout import RingOut, RingOutConfig
        if self.ringout and self.ringout.running:
            self.ringout.stop()
            self.ro_btn.configure(text="Start ring-out")
            return
        if not self.running:
            messagebox.showerror(APP, "Press Start first so the app is listening.")
            return
        row = next((r for r in self.rows if r["target"] == self.ro_target.get()), None)
        if row is None:
            messagebox.showerror(APP, "Pick which channel's fader to ride.")
            return
        try:
            cfg = RingOutConfig(**{k: float(v.get()) for k, v in self.ro_vars.items()})
            if cfg.max_db > 10 or cfg.step_db > 3 or cfg.step_s < 0.5:
                raise ValueError("keep Max at +10 dB or less, Step at 3 dB or less, and Every at 0.5 s or more")
        except (ValueError, tk.TclError) as e:
            messagebox.showerror(APP, f"Check the ring-out settings: {e}")
            return
        if not messagebox.askyesno(APP, f"Ring out {row['target']}: its fader will go to {cfg.start_db:+g} dB and "
                                        f"climb to at most {cfg.max_db:+g} dB. Turn the system down to a safe "
                                        f"level first. Carry on?"):
            return
        self.ro_row = row
        self.ringout = RingOut(row["desk"], cfg, log=lambda m: self.q.put(("log", m)))
        try:
            self.ringout.start()
        except ValueError as e:
            messagebox.showerror(APP, str(e))
            self.ringout = None
            return
        self.ro_btn.configure(text="STOP ring-out")

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
            sup = Suppressor(desk, DetectorConfig(sample_rate=sr), NotchConfig(bands=[1, 2, 3, 4]),
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
        ("Feedback: one manual test notch (dry run)", "feedback notch --desk qu --target Ip1 --band 3 --freq 2500 --gain -6"),
        ("Feedback: one test GEQ cut on Mix1 (dry run; add --apply --host IP to send)", "feedback notch --desk qu-geq --target Mix1 --band 20 --freq 2500 --gain -6"),
    ]

    def _build_tools(self, tab):
        pad = {"padx": 6, "pady": 4}
        f = ttk.Frame(tab)
        f.pack(fill="x", **pad)
        ttk.Label(f, text="Desk IP").grid(row=0, column=0, sticky="e", **pad)
        self.tool_host = tk.StringVar()
        ttk.Entry(f, textvariable=self.tool_host, width=18).grid(row=0, column=1, sticky="w", **pad)
        self.tool_host.trace_add("write", lambda *_: self._fill_tool())
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
        self.tool_cmd.set(tmpl.replace("{host}", self.tool_host.get().strip() or (self.host_var.get().strip() if hasattr(self, "host_var") else "") or "<desk-ip>"))

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
        if self.running or self.ringout:
            self._show_notches()
        self.root.after(150, self._pump)

    def _save_settings(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        data = {k: getattr(self, k + "_var").get() for k in
                ("dev", "chan", "desk", "host", "target", "bands", "midi", "sens", "floor")}
        data["rows"] = [{k: r[k] for k in ("chan", "target", "bands")} for r in self.rows]
        data["ringout"] = {k: v.get() for k, v in self.ro_vars.items()}
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _load_settings(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                data = json.load(f)
            for r in data.pop("rows", []):
                self.rows.append({"chan": int(r["chan"]), "target": r["target"], "bands": r["bands"]})
            for k, v in data.pop("ringout", {}).items():
                if k in self.ro_vars:
                    self.ro_vars[k].set(v)
            self._refresh_rows()
            for k, v in data.items():
                if k == "desk" and v not in DESKS:
                    continue  # desk names changed between versions
                if k == "bands" and v.replace(" ", "") in ("1,2",):
                    v = "2,3"  # old default (0-based LM,HM) -> new 1-based numbers
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


def smoke_drive(root):
    """Runs the real window against a fake audio device: two channels, one with a growing howl."""
    import sounddevice as sd
    from feedback import synth
    global STATE_DIR, SETTINGS, NOTCHES
    import tempfile
    STATE_DIR = tempfile.mkdtemp()
    SETTINGS = os.path.join(STATE_DIR, "settings.json")
    NOTCHES = os.path.join(STATE_DIR, "notches.json")
    messagebox.askyesno = lambda *a, **k: True
    messagebox.showerror = lambda *a, **k: print("showerror:", *a[1:])
    messagebox.showinfo = lambda *a, **k: print("showinfo:", *a[1:])

    class FakeStream:
        def __init__(self, device, channels, samplerate, blocksize, dtype, callback):
            self.cb, self.n, self.sr, self.bs, self.on = callback, channels, samplerate, blocksize, False
            sig = np.stack([synth.speechlike(8) + synth.howl(8, 2500.0), synth.speechlike(8, seed=3)], axis=1)
            self.sig = sig[:, :channels].astype(np.float32)

        def start(self):
            self.on = True
            threading.Thread(target=self._run, daemon=True).start()

        def _run(self):
            i = 0
            while self.on and i + self.bs <= len(self.sig):
                self.cb(self.sig[i:i + self.bs], self.bs, None, None)
                i += self.bs
                time.sleep(self.bs / self.sr / 4)  # 4x real time

        def close(self):
            self.on = False

    sd.InputStream = FakeStream
    app = App(root)
    app.devices = [(0, {"name": "fake", "max_input_channels": 2, "default_samplerate": 48000})]
    app.dev_var.set("0: fake")
    app.desk_var.set("Dry run (no desk)")
    app.chan_var.set(1); app.target_var.set("Ip1"); app.bands_var.set("2,3"); app.add_row()
    app.chan_var.set(2); app.target_var.set("Mix1"); app.bands_var.set("1,2,3,4"); app.add_row()
    app.start()
    app.ro_target.set("Mix1")
    for k, v in (("start_db", -20), ("max_db", -15), ("step_s", 0.5)):
        app.ro_vars[k].set(v)
    root.after(500, app.toggle_ringout)
    root.after(7000, app.stop)
    return app


def main():
    if "--tool" in sys.argv:
        i = sys.argv.index("--tool")
        sys.exit(run_tool_mode(sys.argv[i + 1:]))
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--smoke-gui" in sys.argv:  # build check: drive the window with a fake 2-channel audio device
        out = os.environ.get("SELFTEST_OUT", "smoke.txt")
        root = tk.Tk()
        try:
            app = smoke_drive(root)
        except Exception:
            import traceback
            with open(out, "w") as f:
                f.write(traceback.format_exc())
            raise

        def done():
            text = app.log_txt.get("1.0", "end")
            problems = [w for w in ("Started: in 1 -> Ip1, in 2 -> Mix1", "[ring-out] Mix1", "Saved cuts")
                        if w not in text]
            if "error:" in text:
                problems.append("an error was logged")
            with open(out, "w") as f:
                f.write("OK" if not problems else "FAIL: " + "; ".join(problems) + "\n" + text)
            root.destroy()
        root.after(9000, done)
        root.mainloop()
        return
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
