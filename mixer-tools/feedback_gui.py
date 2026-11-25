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


class Tooltip:
    """Shows a short explanation when the mouse rests on a widget."""

    def __init__(self, widget, text, delay=450):
        self.widget, self.text, self.delay = widget, text, delay
        self.win = None
        self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self.job = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None

    def _show(self):
        if self.win or not self.text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.win = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        try:
            tw.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(tw, bg="#2b2b2b", padx=10, pady=7, highlightthickness=1, highlightbackground="#5a5a5a")
        frame.pack()
        tk.Label(frame, text=self.text, bg="#2b2b2b", fg="#f3f3f3", justify="left", wraplength=360,
                 font=("Segoe UI", 9)).pack()

    def _hide(self, _e=None):
        self._cancel()
        if self.win:
            self.win.destroy()
            self.win = None


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(1120, sw - 40), min(860, sh - 80)
        root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        root.minsize(min(900, w), min(640, h))
        try:
            import sv_ttk  # Windows 11 style theme
            sv_ttk.set_theme("dark")
        except Exception:
            # No theme package: make the custom style names exist so widgets still build.
            st = ttk.Style(root)
            st.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
            st.layout("Switch.TCheckbutton", st.layout("TCheckbutton"))
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
    def _tip(self, widget, text):
        Tooltip(widget, text)
        return widget

    def _card(self, parent, title, subtitle=None):
        outer = ttk.LabelFrame(parent, text=f"  {title}  ", padding=(12, 8))
        if subtitle:
            ttk.Label(outer, text=subtitle, style="Muted.TLabel", wraplength=700, justify="left").pack(anchor="w", pady=(0, 6))
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        return outer, body

    def _build(self):
        T = self._tip
        root = self.root
        style = ttk.Style()
        style.configure("Muted.TLabel", foreground="#8a8a8a")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 17))
        style.configure("Sub.TLabel", foreground="#8a8a8a", font=("Segoe UI", 10))
        style.configure("Big.TButton", font=("Segoe UI Semibold", 12), padding=(22, 10))
        style.configure("Pill.TLabel", font=("Segoe UI Semibold", 10), padding=(10, 3))

        # Header
        head = ttk.Frame(root, padding=(16, 12, 16, 4))
        head.pack(fill="x")
        ttk.Label(head, text="Mixer Tools", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="   feedback control for Qu-16 and DM7", style="Sub.TLabel").pack(side="left", pady=(6, 0))
        self.theme_btn = T(ttk.Button(head, text="Light mode", command=self.toggle_theme),
                           "Switch between the dark and light look.")
        self.theme_btn.pack(side="right")
        self.mode_pill = tk.Label(head, text="  DRY RUN  ", bg="#3a3a3a", fg="#ffffff", font=("Segoe UI Semibold", 9))
        self.mode_pill.pack(side="right", padx=10)
        T(self.mode_pill, "DRY RUN: nothing is sent to the desk, the log shows what would be sent.\n"
                          "LIVE: the app is changing EQ and faders on the desk.")

        nb = self.nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=12, pady=(4, 0))
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  Feedback suppressor  ")
        rotab = ttk.Frame(nb, padding=8)
        nb.add(rotab, text="  Ring out  ")
        tools = ttk.Frame(nb, padding=8)
        nb.add(tools, text="  Desk tools  ")
        self._build_tools(tools)

        # ---- top row: three cards
        top = ttk.Frame(tab)
        top.pack(fill="x")
        for i in range(3):
            top.columnconfigure(i, weight=1, uniform="cards")

        c1, desk = self._card(top, "1  Desk", "Which desk to control and how to reach it.")
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.desk_var = tk.StringVar(value=list(DESKS)[0])
        ttk.Label(desk, text="Desk and EQ").grid(row=0, column=0, sticky="w")
        T(ttk.Combobox(desk, textvariable=self.desk_var, values=list(DESKS), state="readonly", width=30),
          "Dry run: no desk, just shows what would happen.\n"
          "Qu-16 PEQ: cuts on the parametric EQ of any channel or mix.\n"
          "Qu-16 GEQ: cuts on the 28-band graphic EQ (mixes and LR only).\n"
          "Yamaha DM7 PEQ: cuts on the DM7's parametric EQ.").grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 8))
        ttk.Label(desk, text="Desk IP address").grid(row=2, column=0, sticky="w")
        ttk.Label(desk, text="Qu MIDI ch").grid(row=2, column=1, sticky="w", padx=(8, 0))
        self.host_var = tk.StringVar()
        T(ttk.Entry(desk, textvariable=self.host_var, width=18),
          "The desk's network address, e.g. 192.168.1.50.\nQu: Setup > Network.  DM7: SETUP > NETWORK > For Mixer Control.").grid(row=3, column=0, sticky="we")
        self.midi_var = tk.IntVar(value=1)
        T(ttk.Spinbox(desk, from_=1, to=16, textvariable=self.midi_var, width=5),
          "The Qu's MIDI channel. The app reads it from the desk when it connects, so you rarely need to change this.").grid(row=3, column=1, sticky="w", padx=(8, 0))
        self.apply_var = tk.BooleanVar(value=False)
        T(ttk.Checkbutton(desk, text="Write to the desk", variable=self.apply_var, style="Switch.TCheckbutton",
                          command=self._update_pill),
          "Off: safe dry run, nothing is sent.\nOn: the app really changes EQ (and faders in ring-out) on the desk.").grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        desk.columnconfigure(0, weight=1)

        c2, audio = self._card(top, "2  Audio input", "Where the app listens. Use a feed taken after the EQ, such as Qu USB or Dante.")
        c2.grid(row=0, column=1, sticky="nsew", padx=6)
        self.dev_var = tk.StringVar()
        self.dev_box = T(ttk.Combobox(audio, textvariable=self.dev_var, state="readonly", width=34),
                         "The sound card or desk USB/Dante interface carrying the channels you want to protect.\n"
                         "The feed must be after the EQ (e.g. a direct out at pre-fader), or the app can't hear its own cuts.")
        self.dev_box.grid(row=0, column=0, sticky="we")
        T(ttk.Button(audio, text="Refresh", command=self.refresh_devices),
          "Look for audio devices again (e.g. after plugging in the desk's USB).").grid(row=0, column=1, padx=(6, 0))
        audio.columnconfigure(0, weight=1)

        c3, tune = self._card(top, "Detection", "How eager the app is to call something feedback.")
        c3.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        ttk.Label(tune, text="Sensitivity").grid(row=0, column=0, sticky="w")
        self.sens_var = tk.DoubleVar(value=16.0)
        T(ttk.Scale(tune, from_=24, to=10, variable=self.sens_var),
          "Left: only acts on loud, obvious howls.\nRight: catches feedback earlier, but may also catch steady notes.\nThe middle (16) suits most rooms.").grid(row=1, column=0, sticky="we", pady=(0, 8))
        ttk.Label(tune, text="Ignore anything quieter than (dBFS)").grid(row=2, column=0, sticky="w")
        self.floor_var = tk.IntVar(value=-50)
        T(ttk.Spinbox(tune, from_=-90, to=-10, increment=5, textvariable=self.floor_var, width=6),
          "Steady tones quieter than this are ignored. Hum and room tone usually sit below -50 dBFS.").grid(row=3, column=0, sticky="w")
        tune.columnconfigure(0, weight=1)

        # ---- channels card
        c4, rows = self._card(tab, "3  Channels to watch",
                              "Each row listens to one input channel and cuts feedback on its own desk channel or mix.")
        c4.pack(fill="x", pady=(10, 0))
        ed = ttk.Frame(rows)
        ed.pack(fill="x")
        ttk.Label(ed, text="Input ch").pack(side="left")
        self.chan_var = tk.IntVar(value=1)
        T(ttk.Spinbox(ed, from_=1, to=64, textvariable=self.chan_var, width=4),
          "Which channel of the audio input to listen to (1 = first USB/Dante channel).").pack(side="left", padx=(4, 14))
        ttk.Label(ed, text="Desk target").pack(side="left")
        self.target_var = tk.StringVar(value="Ip1")
        T(ttk.Entry(ed, textvariable=self.target_var, width=9),
          "Which desk channel gets the cuts.\nQu: Ip1-Ip32, ST1-ST3, Mix1-Mix4, LR.\nDM7: InCh:0, Mix:3, Mtrx:1 (counting from 0).").pack(side="left", padx=(4, 14))
        ttk.Label(ed, text="PEQ bands").pack(side="left")
        self.bands_var = tk.StringVar(value="2,3")
        T(ttk.Entry(ed, textvariable=self.bands_var, width=8),
          "Which parametric EQ bands the app may use, numbered as on the desk:\n1 = LF, 2 = LM, 3 = HM, 4 = HF.\n"
          "2,3 leaves your low and high shelves alone. Ignored in GEQ mode.").pack(side="left", padx=(4, 14))
        T(ttk.Button(ed, text="Add / update", style="Accent.TButton", command=self.add_row),
          "Add this row, or update the row with the same desk target.").pack(side="left")
        T(ttk.Button(ed, text="Remove", command=self.remove_row),
          "Remove the selected row.").pack(side="left", padx=(6, 0))
        self.rows_tv = ttk.Treeview(rows, columns=("chan", "target", "bands", "level", "cuts"), show="headings", height=4)
        for col, title, w in (("chan", "Input ch", 80), ("target", "Desk target", 100), ("bands", "PEQ bands", 90),
                              ("level", "Level", 90), ("cuts", "Cuts on the desk", 460)):
            self.rows_tv.heading(col, text=title)
            self.rows_tv.column(col, width=w, anchor="w")
        self.rows_tv.pack(fill="x", pady=(8, 0))
        T(self.rows_tv, "The channels being watched. Level shows the live input level; "
                        "Cuts shows every EQ cut the app has made. Click a row to edit it.")
        self.rows_tv.bind("<<TreeviewSelect>>", lambda e: self._edit_selected())
        self.rows = []

        # ---- action bar
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=12)
        self.start_btn = T(ttk.Button(bar, text="Start", style="Accent.TButton", command=self.toggle, width=12),
                           "Start listening and cutting feedback on every row. Press again to stop.\n"
                           "Cuts are saved when you stop.")
        self.start_btn.pack(side="left", ipady=6)
        self.freeze_var = tk.BooleanVar(value=False)
        T(ttk.Checkbutton(bar, text="Freeze", variable=self.freeze_var, style="Switch.TCheckbutton",
                          command=self._apply_freeze),
          "Keep listening and logging, but stop making new cuts. Handy during the show once the room is rung out.").pack(side="left", padx=16)
        T(ttk.Button(bar, text="Clear cuts", command=self.clear),
          "Set every cut the app made back to flat on the desk.").pack(side="left")
        T(ttk.Button(bar, text="Try a simulation", command=self.simulate),
          "Runs a pretend room that howls at 2 kHz, so you can see the app work without a desk.").pack(side="right")

        # ---- activity log
        c5, lf = self._card(tab, "Activity")
        c5.pack(fill="both", expand=True)
        self.log_txt = tk.Text(lf, height=7, wrap="none", font=("Cascadia Mono", 9), state="disabled",
                               relief="flat", borderwidth=0, padx=8, pady=6)
        sb = ttk.Scrollbar(lf, command=self.log_txt.yview)
        self.log_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log_txt.pack(fill="both", expand=True)

        self._build_ringout(rotab)

        # ---- status bar
        sbar = ttk.Frame(root, padding=(16, 6))
        sbar.pack(fill="x")
        self.status = ttk.Label(sbar, text="Stopped", style="Muted.TLabel")
        self.status.pack(side="left")
        ttk.Label(sbar, text="Hover over anything for help", style="Muted.TLabel").pack(side="right")
        self._text_widgets = [self.log_txt, self.tool_out]
        self._apply_text_colours()

    def _build_ringout(self, tab):
        T = self._tip
        c, body = self._card(tab, "Ring out a channel",
                             "The app raises a fader a little at a time. Whenever something starts to ring it pulls the fader "
                             "back, cuts that frequency, waits, and carries on, until it reaches your maximum. "
                             "Press Start on the first tab before you begin.")
        c.pack(fill="x")
        ttk.Label(body, text="Fader to ride").grid(row=0, column=0, sticky="w")
        self.ro_target = tk.StringVar()
        self.ro_box = T(ttk.Combobox(body, textvariable=self.ro_target, state="readonly", width=12),
                        "Which watched channel's fader to bring up. Its cuts go on the same channel.\n"
                        "For a wedge, choose its mix (e.g. Mix1) so the mix master is raised.")
        self.ro_box.grid(row=1, column=0, sticky="w", padx=(0, 18))
        self.ro_vars = {}
        fields = (("start_db", "Start at (dB)", -30.0, "The fader is set here first. A safe low level."),
                  ("max_db", "Never above (dB)", 0.0, "The highest the fader will ever go. Capped at +10 dB."),
                  ("step_db", "Step (dB)", 1.0, "How much the fader rises each step. Up to 3 dB."),
                  ("step_s", "Every (s)", 2.0, "How long to wait between steps, so feedback has time to show."),
                  ("backoff_db", "Back off (dB)", 4.0, "How far the fader drops straight away when something rings."),
                  ("guard_dbfs", "Safety limit (dBFS)", -3.0,
                   "If the input ever gets this loud, the fader drops 10 dB and ring-out stops."))
        for n, (key, label, default, tip) in enumerate(fields, start=1):
            r, i = 2 * (n // 4), n % 4  # four per row so it fits a laptop screen
            ttk.Label(body, text=label).grid(row=r, column=i, sticky="w", padx=(0, 14), pady=(8 if r else 0, 0))
            v = tk.DoubleVar(value=default)
            T(ttk.Spinbox(body, textvariable=v, from_=-90, to=10, increment=0.5, width=7), tip).grid(
                row=r + 1, column=i, sticky="w", padx=(0, 14))
            self.ro_vars[key] = v

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=16)
        self.ro_btn = T(ttk.Button(act, text="Start ring-out", style="Accent.TButton", command=self.toggle_ringout, width=16),
                        "Begin raising the fader. Press again (STOP) at any time: the fader goes back to the start level.")
        self.ro_btn.pack(side="left", ipady=6)
        self.ro_level = ttk.Progressbar(act, maximum=100, length=360)
        self.ro_level.pack(side="left", padx=16)
        T(self.ro_level, "Where the fader is between the start level and the maximum.")
        self.ro_status = ttk.Label(tab, text="Idle. The fader never goes above your maximum, backs off on every howl, "
                                             "and STOP puts it back to the start level.", style="Muted.TLabel",
                                   wraplength=900, justify="left")
        self.ro_status.pack(anchor="w")
        self.ringout = None
        self.ro_row = None

    # ------------------------------------------------------------- theme
    def toggle_theme(self):
        try:
            import sv_ttk
            dark = sv_ttk.get_theme() == "dark"
            sv_ttk.set_theme("light" if dark else "dark")
            self.theme_btn.configure(text="Dark mode" if dark else "Light mode")
        except Exception:
            return
        self._apply_text_colours()

    def _apply_text_colours(self):
        try:
            import sv_ttk
            dark = sv_ttk.get_theme() == "dark"
        except Exception:
            dark = False
        bg, fg = ("#1f1f1f", "#e4e4e4") if dark else ("#fafafa", "#1b1b1b")
        for w in getattr(self, "_text_widgets", []):
            w.configure(bg=bg, fg=fg, insertbackground=fg)

    def _update_pill(self, live=None):
        if live is None:
            live = self.running and self.apply_var.get() and DESKS.get(self.desk_var.get()) != "dry"
        if live:
            self.mode_pill.configure(text="  LIVE: WRITING TO DESK  ", bg="#c42b1c")
        elif self.apply_var.get() and DESKS.get(self.desk_var.get()) != "dry":
            self.mode_pill.configure(text="  ARMED: WILL WRITE ON START  ", bg="#9d5d00")
        else:
            self.mode_pill.configure(text="  DRY RUN  ", bg="#3a3a3a")

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
            self.ro_status.configure(text=txt)
            c = self.ringout.cfg
            span = max(0.1, c.max_db - c.start_db)
            self.ro_level["value"] = max(0, min(100, 100 * (self.ringout.level - c.start_db) / span))
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
        self.running = True
        self.status.configure(text=f"Listening at {sr} Hz on {len(self.rows)} channel(s) -> {self.desk_var.get()}"
                                   f"{'  (WRITING TO DESK)' if apply else '  (dry run)'}")
        self._update_pill()
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
        self._update_pill()

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
        self.ro_status.configure(text=f"Riding the {row['target']} fader...")

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
        T = self._tip
        card, f = self._card(tab, "Run a desk tool",
                             "Quick checks and probes for the Qu-16 and DM7. Pick a tool, check the command, then press Run.")
        card.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(f, text="Desk IP").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        self.tool_host = tk.StringVar()
        T(ttk.Entry(f, textvariable=self.tool_host, width=18),
          "The desk's IP address. If you leave it empty, the IP from the Feedback tab is used.").grid(
            row=0, column=1, sticky="w", padx=6, pady=4)
        self.tool_host.trace_add("write", lambda *_: self._fill_tool())
        ttk.Label(f, text="Tool").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.tool_pick = tk.StringVar(value=self.TOOL_CMDS[0][0])
        box = T(ttk.Combobox(f, textvariable=self.tool_pick, values=[t[0] for t in self.TOOL_CMDS],
                             state="readonly", width=60),
                "Pick what you want to do. The command box below fills in for you.")
        box.grid(row=1, column=1, columnspan=3, sticky="we", padx=6, pady=4)
        box.bind("<<ComboboxSelected>>", lambda e: self._fill_tool())
        ttk.Label(f, text="Command").grid(row=2, column=0, sticky="e", padx=6, pady=4)
        self.tool_cmd = tk.StringVar()
        T(ttk.Entry(f, textvariable=self.tool_cmd, width=80),
          "The exact command that will run. You can edit it. Nothing is written to a desk unless the command has --apply or is a DM7 'set'.").grid(
            row=2, column=1, columnspan=3, sticky="we", padx=6, pady=4)
        f.columnconfigure(3, weight=1)
        b = ttk.Frame(tab)
        b.pack(fill="x", padx=12, pady=4)
        self.tool_btn = T(ttk.Button(b, text="Run", style="Accent.TButton", width=10, command=self.run_tool),
                          "Run the command. Its output appears below and is also saved to the logs folder.")
        self.tool_btn.pack(side="left", padx=(0, 6))
        T(ttk.Button(b, text="Stop", command=self.stop_tool), "Stop the tool that's running.").pack(side="left", padx=6)
        T(ttk.Button(b, text="Open logs folder", command=lambda: os.startfile(self._logs_dir())
                     if hasattr(os, "startfile") else None),
          "Open the folder with a text file of every run, handy for sending to someone.").pack(side="left", padx=6)
        out = ttk.LabelFrame(tab, text="  Output  ", padding=(8, 6))
        out.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        self.tool_out = tk.Text(out, wrap="none", font=("Consolas", 10), state="disabled", relief="flat",
                                borderwidth=0, padx=8, pady=6)
        self.tool_out.pack(fill="both", expand=True)
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
    root.after(4500, lambda: app.nb.select(1))  # CI takes screenshots at ~3 s and ~5.5 s
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
            if getattr(sys, "frozen", False):
                try:
                    import sv_ttk
                    if sv_ttk.get_theme() not in ("dark", "light"):
                        problems.append("theme not loaded")
                except Exception as e:
                    problems.append(f"theme missing: {e}")
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
