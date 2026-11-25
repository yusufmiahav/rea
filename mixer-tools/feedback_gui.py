#!/usr/bin/env python3
"""Windowed front end for the feedback suppressor (this is what the .exe runs)."""
import json
import math
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

from feedback.desks import QU_BAND_NAMES, QU_GEQ_FREQS, DryRun, Dm7Desk, QuDesk, QuGeqDesk, QuSplitDesk, load_calibration
from feedback.detector import SPEEDS, DetectorConfig, speed_config
from feedback.notches import GEQ_OFFSET, GeqManager, NotchConfig, NotchManager, SplitManager
from feedback import rta
from feedback import qu_routing
from feedback.suppressor import Suppressor

APP = "Mixer Tools"
STATE_DIR = os.path.join(os.path.expanduser("~"), "MixerTools")
SETTINGS = os.path.join(STATE_DIR, "settings.json")
NOTCHES = os.path.join(STATE_DIR, "notches.json")
DESKS = {"Dry run (no desk)": "dry", "Qu-16 PEQ (any channel or mix)": "qu",
         "Qu-16 GEQ (mixes and LR only)": "qu-geq",
         "Qu-16 PEQ + GEQ together (mixes and LR only)": "qu-split", "Yamaha DM7 PEQ": "dm7"}
MAX_DEPTH = {"dry": -12.0, "qu": -12.0, "qu-geq": -12.0, "qu-split": -12.0, "dm7": -18.0}


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
        self.meter_stream = None   # meters-only feed, when the suppressor isn't running
        self.meter_lv = None       # latest peak level per audio channel, dBFS
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
            sub = ttk.Label(outer, text=subtitle, style="Muted.TLabel", wraplength=300, justify="left")
            sub.pack(anchor="w", fill="x", pady=(0, 6))
            sub.bind("<Configure>", lambda e: e.widget.configure(wraplength=max(120, e.width - 4)))
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
        rtatab = ttk.Frame(nb, padding=8)
        nb.add(rtatab, text="  Analyser (RTA)  ")
        metertab = ttk.Frame(nb, padding=8)
        nb.add(metertab, text="  Meters  ")
        rotab = ttk.Frame(nb, padding=8)
        nb.add(rotab, text="  Ring out  ")
        tools = ttk.Frame(nb, padding=8)
        routetab = ttk.Frame(nb, padding=8)
        nb.add(routetab, text="  Qu routing  ")
        ctltab = ttk.Frame(nb, padding=8)
        nb.add(ctltab, text="  Desk control  ")
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
        lowrow = ttk.Frame(tune)
        lowrow.grid(row=3, column=0, sticky="w")
        T(ttk.Spinbox(lowrow, from_=-90, to=-10, increment=5, textvariable=self.floor_var, width=6),
          "Steady tones quieter than this are ignored. Hum and room tone usually sit below -50 dBFS.").pack(side="left")
        hpfrow = ttk.Frame(tune)
        hpfrow.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.hpf_var = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(hpfrow, text="Raise HPF up to", variable=self.hpf_var, style="Switch.TCheckbutton"),
          "Low howls (rumble, boomy wedges) are handled by raising the channel's high-pass filter\n"
          "instead of using up a PEQ band. It only ever goes up, never past the limit you set here,\n"
          "and if it reaches the limit a PEQ band takes over. Inputs only: mixes and LR have no HPF.").pack(side="left", padx=(0, 6))
        self.hpfmax_var = tk.IntVar(value=160)
        T(ttk.Spinbox(hpfrow, from_=40, to=400, increment=10, textvariable=self.hpfmax_var, width=5),
          "The highest the app may set the HPF, in Hz. 120-160 is safe for vocals; keep it lower for kick, bass or male voices.").pack(side="left")
        ttk.Label(hpfrow, text="Hz").pack(side="left", padx=(4, 0))
        speedrow = ttk.Frame(tune)
        speedrow.grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Label(speedrow, text="Response").pack(side="left", padx=(0, 6))
        self.speed_var = tk.StringVar(value="normal")
        T(ttk.Combobox(speedrow, textvariable=self.speed_var, values=list(SPEEDS), state="readonly", width=10),
          "How quickly the app acts on a ringing frequency.\n"
          "normal: waits 0.5 s to be sure, so speech and singing are left alone.\n"
          "fast: acts after about 0.25 s, is more sensitive, and deepens a cut sooner.\n"
          "very fast: acts after about 0.1 s. Best for ringing out or loud wedges; it may catch a long held note.\n"
          "Takes effect when you press Start.").pack(side="left")
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
        self.recover_var = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(bar, text="Recover cuts", variable=self.recover_var, style="Switch.TCheckbutton",
                          command=self._apply_recover),
          "When a cut frequency has been quiet for 8 seconds, ease the cut back 1 dB every 2 seconds until it's flat\n"
          "and the band is free again. If it rings again on the way up, it drops back and stays at the depth that worked.\n"
          "Each channel can have at most 18 dB of cut in total, so the EQ stays gentle.").pack(side="left", padx=(0, 16))
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
        self._build_rta(rtatab)
        self._build_meters(metertab)
        self._build_control(ctltab)
        self._build_routing(routetab)

        # ---- status bar
        sbar = ttk.Frame(root, padding=(16, 6))
        sbar.pack(fill="x")
        self.status = ttk.Label(sbar, text="Stopped", style="Muted.TLabel")
        self.status.pack(side="left")
        ttk.Label(sbar, text="Hover over anything for help", style="Muted.TLabel").pack(side="right")
        self._text_widgets = [self.log_txt, self.tool_out, self.rt_log]
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

    # ------------------------------------------------------------- Qu routing
    def _build_routing(self, tab):
        T = self._tip
        top = ttk.Frame(tab)
        top.pack(fill="both", expand=True)
        c1, b1 = self._card(top, "1  Pick the channels",
                            "Ctrl-click or Shift-click to pick several. Press Read from desk to see how they're routed now.")
        c1.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.rt_tree = ttk.Treeview(b1, columns=("ch", "lr", "sends"), show="headings", height=8, selectmode="extended")
        for col, text, w in (("ch", "Channel", 80), ("lr", "On L+R", 70), ("sends", "Sent to (from the desk)", 220)):
            self.rt_tree.heading(col, text=text)
            self.rt_tree.column(col, width=w, anchor="w")
        sb = ttk.Scrollbar(b1, command=self.rt_tree.yview)
        self.rt_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.rt_tree.pack(fill="both", expand=True)
        T(self.rt_tree, "The desk's input channels. Pick the ones you want to move onto the mix.")
        for name in qu_routing.input_names(32):
            self.rt_tree.insert("", "end", iid=name, values=(name, "?", ""))

        right = ttk.Frame(top)
        right.pack(side="left", fill="both")
        c2, b2 = self._card(right, "2  Send them to a mix", "Post-fade, so the channel faders still set the balance.")
        c2.pack(fill="x")
        ttk.Label(b2, text="Mix").grid(row=0, column=0, sticky="w")
        self.rt_mix = tk.StringVar(value="Mix1")
        T(ttk.Combobox(b2, textvariable=self.rt_mix, values=list(qu_routing.MIX_CODES), state="readonly", width=10),
          "The mix that collects the channels. Its own PEQ and GEQ then do the feedback work,\n"
          "so the L+R EQ stays clean.").grid(row=1, column=0, sticky="w", padx=(0, 12))
        ttk.Label(b2, text="Send level (dB)").grid(row=0, column=1, sticky="w")
        self.rt_send = tk.DoubleVar(value=0.0)
        T(ttk.Spinbox(b2, from_=-40, to=10, increment=1, textvariable=self.rt_send, width=6),
          "How much of each channel goes into the mix. 0 dB = the same level as on its fader.").grid(row=1, column=1, sticky="w")
        self.rt_post = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(b2, text="Post-fade", variable=self.rt_post, style="Switch.TCheckbutton"),
          "On: the send follows the channel fader (what you want here).\nOff: pre-fade, the send ignores the fader.").grid(
            row=2, column=0, sticky="w", pady=(8, 0))
        self.rt_offlr = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(b2, text="Take them off L+R", variable=self.rt_offlr, style="Switch.TCheckbutton"),
          "Unassigns the channels from L+R, so they only reach the PA through the mix and its return.").grid(
            row=2, column=1, sticky="w", pady=(8, 0))
        self.rt_clear = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(b2, text="Take every other channel off this mix", variable=self.rt_clear,
                          style="Switch.TCheckbutton"),
          "Unassigns all the channels you didn't pick from this mix (including the return input),\n"
          "so only your picked channels feed it.").grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        c3, b3 = self._card(right, "3  Bring the mix back",
                            "Patch the mix output to a socket and cable it into a spare input (or loop it through USB). "
                            "That input goes on L+R.")
        c3.pack(fill="x", pady=(8, 0))
        ttk.Label(b3, text="Return input").grid(row=0, column=0, sticky="w")
        self.rt_ret = tk.StringVar(value="(none)")
        T(ttk.Combobox(b3, textvariable=self.rt_ret, values=["(none)"] + qu_routing.input_names(32),
                       state="readonly", width=10),
          "The input the mix comes back on. It's put on L+R at the fader level you set.\n"
          "It must not be one of the channels you're sending, or you'll make a loop.").grid(row=1, column=0, sticky="w", padx=(0, 12))
        ttk.Label(b3, text="Return fader (dB)").grid(row=0, column=1, sticky="w")
        self.rt_retdb = tk.DoubleVar(value=0.0)
        T(ttk.Spinbox(b3, from_=-40, to=10, increment=1, textvariable=self.rt_retdb, width=6),
          "Where the return channel's fader is set.").grid(row=1, column=1, sticky="w")
        self.rt_mixfader = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(b3, text="Set the mix master to 0 dB", variable=self.rt_mixfader, style="Switch.TCheckbutton"),
          "Puts the mix's own master fader at 0 dB so the return comes back at unity.").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        act = ttk.Frame(tab)
        act.pack(fill="x", pady=(10, 6))
        T(ttk.Button(act, text="Read from desk", command=self.routing_read),
          "Asks the desk for its current settings and fills in the On L+R and Sent to columns.\n"
          "Needs Write to the desk on and the desk IP on the first tab.").pack(side="left")
        T(ttk.Button(act, text="Send to mix", style="Accent.TButton", command=self.routing_apply, width=14),
          "Does steps 2 and 3 for the channels you picked. With Write to the desk off, it only shows what it would send.").pack(
            side="left", padx=8, ipady=4)
        T(ttk.Button(act, text="Put back on L+R", command=self.routing_undo),
          "Undo for the picked channels: back on L+R and off the mix.").pack(side="left")
        T(ttk.Button(act, text="Watch this mix for feedback", command=self.routing_watch),
          "Fills in the Feedback tab so the suppressor works on this mix's EQ (or the return channel's).\n"
          "Then set Input ch to the USB channel carrying it, and press Add.").pack(side="right")
        self.rt_status = ttk.Label(tab, style="Muted.TLabel", wraplength=900, justify="left",
                                   text="Uses the desk, IP and MIDI channel from the Feedback tab. Qu only.")
        self.rt_status.pack(anchor="w")
        self.rt_log = tk.Text(tab, height=5, wrap="none", font=("Cascadia Mono", 9), state="disabled",
                              relief="flat", borderwidth=0, padx=8, pady=4)
        self.rt_log.pack(fill="both", expand=True, pady=(6, 0))
        T(self.rt_log, "Every message sent to the desk for routing, byte by byte, so you can check it.")

    # ------------------------------------------------------- desk control
    def _build_control(self, tab):
        T = self._tip
        self.ctl = None            # QuControl / Dm7Control
        self.ctl_owned = None      # a desk connection this page opened itself
        self.ctl_pending = {}
        self.ctl_loading = False
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Channel").pack(side="left")
        self.ctl_target = tk.StringVar(value="Ip1")
        self.ctl_box = T(ttk.Combobox(bar, textvariable=self.ctl_target, state="readonly", width=12),
                         "The desk channel or mix to control. Qu: Ip1-16, ST1-3, Mix1-4, Mix5-6..9-10, LR.\n"
                         "DM7: InCh:0.. and Mix:0.. (0-based).")
        self.ctl_box.pack(side="left", padx=(8, 12))
        self.ctl_box.bind("<<ComboboxSelected>>", lambda e: self._ctl_select())
        T(ttk.Button(bar, text="Connect", command=self.ctl_connect),
          "Connect to the desk chosen on the Feedback tab (IP and Write to the desk).\n"
          "If the suppressor is running, its connection is shared.").pack(side="left")
        T(ttk.Button(bar, text="Read from desk", command=self.ctl_read),
          "Ask the desk for its current settings and move the controls to match (Qu only).").pack(side="left", padx=6)
        self.ctl_status = ttk.Label(bar, style="Muted.TLabel", text="Not connected. Changes are only shown in the log until you connect.")
        self.ctl_status.pack(side="left", padx=12)

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True)
        for i, wt in enumerate((1, 3, 3)):
            body.columnconfigure(i, weight=wt, uniform="ctl")
        body.rowconfigure(0, weight=1)
        v = self.ctlv = {}

        # Strip: fader, mute, pan, preamp
        c1, strip = self._card(body, "Channel")
        c1.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        v["mute"] = tk.BooleanVar(value=False)
        self.ctl_mute_btn = T(ttk.Checkbutton(strip, text="Mute", variable=v["mute"], style="Switch.TCheckbutton",
                                              command=lambda: self._ctl("mute", lambda c, t: c.mute(t, v["mute"].get()))),
                              "Mute the channel on the desk.")
        self.ctl_mute_btn.pack(anchor="w")
        v["lr"] = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(strip, text="On L+R", variable=v["lr"], style="Switch.TCheckbutton",
                          command=lambda: self._ctl("lr", lambda c, t: c.lr_assign(t, v["lr"].get()))),
          "Whether this channel goes to the main L+R mix.").pack(anchor="w", pady=(4, 8))
        frow = ttk.Frame(strip)
        frow.pack(fill="both", expand=True)
        v["fader"] = tk.DoubleVar(value=0.0)
        T(ttk.Scale(frow, from_=10, to=-60, orient="vertical", variable=v["fader"],
                    command=lambda e: self._ctl("fader", lambda c, t: c.fader(t, self._fader_db()))),
          "Channel fader. Bottom is -inf (off), 0 dB is unity, top is +10 dB.").pack(side="left", fill="y", expand=True)
        self.ctl_fader_lbl = ttk.Label(frow, text="0.0 dB", width=8)
        self.ctl_fader_lbl.pack(side="left", padx=6)
        T(ttk.Button(strip, text="0 dB", command=lambda: (v["fader"].set(0.0), self._ctl("fader", lambda c, t: c.fader(t, 0.0)))),
          "Set the fader to unity (0 dB).").pack(fill="x", pady=(6, 0))
        ttk.Label(strip, text="Pan  L - C - R").pack(anchor="w", pady=(10, 0))
        v["pan"] = tk.DoubleVar(value=0.0)
        T(ttk.Scale(strip, from_=-1, to=1, variable=v["pan"],
                    command=lambda e: self._ctl("pan", lambda c, t: c.pan(t, v["pan"].get()))),
          "Pan to L+R. Double-click to centre.").pack(fill="x")
        self.ctl_in = ttk.Frame(strip)
        self.ctl_in.pack(fill="x", pady=(10, 0))
        ttk.Label(self.ctl_in, text="Preamp gain").pack(anchor="w")
        v["gain"] = tk.DoubleVar(value=20.0)
        T(ttk.Scale(self.ctl_in, from_=-5, to=60, variable=v["gain"],
                    command=lambda e: self._ctl("gain", lambda c, t: c.gain(t, v["gain"].get()))),
          "Mic preamp gain on the desk's rear-panel input (-5 to +60 dB).").pack(fill="x")
        self.ctl_gain_lbl = ttk.Label(self.ctl_in, text="", style="Muted.TLabel")
        self.ctl_gain_lbl.pack(anchor="w")
        v["phantom"] = tk.BooleanVar(value=False)
        T(ttk.Checkbutton(self.ctl_in, text="48V phantom", variable=v["phantom"], style="Switch.TCheckbutton",
                          command=self._ctl_phantom),
          "Phantom power for condenser mics. Mute the channel first: switching 48V makes a loud pop.").pack(anchor="w", pady=(6, 0))

        # EQ: HPF + 4-band PEQ
        c2, eq = self._card(body, "EQ")
        c2.grid(row=0, column=1, sticky="nsew", padx=6)
        hrow = ttk.Frame(eq)
        hrow.pack(fill="x")
        v["hpf_in"] = tk.BooleanVar(value=False)
        self.ctl_hpf_sw = T(ttk.Checkbutton(hrow, text="HPF", variable=v["hpf_in"], style="Switch.TCheckbutton",
                                            command=lambda: self._ctl("hpf", self._ctl_hpf_fn)),
                            "High-pass filter (inputs only).")
        self.ctl_hpf_sw.pack(side="left")
        v["hpf_hz"] = tk.DoubleVar(value=80.0)
        self.ctl_hpf_scale = T(ttk.Scale(hrow, from_=20, to=400, variable=v["hpf_hz"],
                                         command=lambda e: self._ctl("hpf", self._ctl_hpf_fn)),
                               "HPF frequency, 20-400 Hz.")
        self.ctl_hpf_scale.pack(side="left", fill="x", expand=True, padx=8)
        self.ctl_hpf_lbl = ttk.Label(hrow, text="80 Hz", width=7)
        self.ctl_hpf_lbl.pack(side="left")
        v["peq_in"] = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(eq, text="PEQ in", variable=v["peq_in"], style="Switch.TCheckbutton",
                          command=lambda: self._ctl("peq_in", lambda c, t: c.peq_in(t, v["peq_in"].get()))),
          "Switch the 4-band parametric EQ in or out.").pack(anchor="w", pady=(10, 4))
        grid = ttk.Frame(eq)
        grid.pack(fill="both", expand=True)
        for col, head in enumerate(("", "Gain", "Freq", "Width", "")):
            ttk.Label(grid, text=head, style="Muted.TLabel").grid(row=0, column=col, sticky="w")
        for col in (1, 2, 3):
            grid.columnconfigure(col, weight=1)
        self.ctl_band_lbl = {}
        from feedback import control as ctlmod
        defaults = {1: 100.0, 2: 400.0, 3: 2500.0, 4: 8000.0}
        for b in (1, 2, 3, 4):
            ttk.Label(grid, text=QU_BAND_NAMES[b]).grid(row=b, column=0, sticky="w", padx=(0, 6), pady=4)
            v[f"g{b}"] = tk.DoubleVar(value=0.0)
            v[f"f{b}"] = tk.DoubleVar(value=ctlmod.slider_from_freq(defaults[b]))
            v[f"w{b}"] = tk.DoubleVar(value=ctlmod.slider_from_width(1.0))
            for col, key, lo, hi in ((1, f"g{b}", -12, 12), (2, f"f{b}", 0, 1), (3, f"w{b}", 0, 1)):
                T(ttk.Scale(grid, from_=lo, to=hi, variable=v[key],
                            command=lambda e, b=b: self._ctl(f"band{b}", lambda c, t, b=b: c.peq_band(t, b, *self._band(b)))),
                  {1: "Gain, -12 to +12 dB.", 2: "Frequency, 20 Hz to 20 kHz.", 3: "Width: left is wide (1.5 octaves), right is narrow (1/9 octave)."}[col]
                  ).grid(row=b, column=col, sticky="we", padx=3)
            self.ctl_band_lbl[b] = ttk.Label(grid, text="", width=22)
            self.ctl_band_lbl[b].grid(row=b, column=4, sticky="w")
        T(ttk.Button(eq, text="Flatten EQ", command=self._ctl_flat),
          "Set all four PEQ gains to 0 dB and, on a mix or LR, every GEQ fader to 0 dB.\n"
          "Asks first. Frequencies, widths and the HPF are left as they are.").pack(anchor="w", pady=(8, 0))

        # Sends
        c3, sends = self._card(body, "Sends", "For an input: its sends to each mix. For a mix: every input's send into it.")
        c3.grid(row=0, column=2, sticky="nsew", padx=(6, 0))
        self.ctl_sends = sends
        self.ctl_send_vars = {}
        log_frame = ttk.Frame(tab)
        log_frame.pack(fill="x", pady=(6, 0))
        self.ctl_log = tk.Text(log_frame, height=3, wrap="none", font=("Cascadia Mono", 9), state="disabled",
                               relief="flat", borderwidth=0, padx=8, pady=4)
        self.ctl_log.pack(fill="x")
        T(self.ctl_log, "Every message sent to the desk from this page.")
        self._ctl_targets()
        self.desk_var.trace_add("write", lambda *a: self._ctl_targets())

    # helpers -------------------------------------------------------------
    def _ctl_kind(self):
        kind = DESKS[self.desk_var.get()]
        return "dm7" if kind == "dm7" else "qu"

    def _ctl_targets(self):
        from feedback import control as ctlmod
        names = (ctlmod.Dm7Control(None).targets() if self._ctl_kind() == "dm7" else ctlmod.qu_targets(16))
        if list(self.ctl_box["values"]) != names:
            self.ctl_box["values"] = names
        if self.ctl_target.get() not in names:
            self.ctl_target.set(names[0])
        self._ctl_select()

    def _ctl_log_line(self, m):
        self.q.put(("log", m))

        def show():
            self.ctl_log.configure(state="normal")
            self.ctl_log.insert("end", time.strftime("%H:%M:%S  ") + m + "\n")
            self.ctl_log.see("end")
            self.ctl_log.configure(state="disabled")
        self.q.put(("call", show))

    def _ctl_control(self):
        """The controller in use: a live one after Connect, else a dry-run one that only logs."""
        from feedback import control as ctlmod
        if self.ctl is not None and self.ctl.name == self._ctl_kind():
            return self.ctl
        if self._ctl_kind() == "dm7":
            return ctlmod.Dm7Control(Dm7Desk(None, "InCh:0", apply=False, log=self._ctl_log_line))
        return ctlmod.QuControl(QuDesk(None, "Ip1", apply=False, log=self._ctl_log_line))

    def ctl_connect(self):
        from feedback import control as ctlmod
        kind = DESKS[self.desk_var.get()]
        try:
            if kind == "dm7":
                if self.desk is not None and isinstance(self.desk, Dm7Desk):
                    import copy
                    d = copy.copy(self.desk)
                    d._owner, d.log = False, self._ctl_log_line
                else:
                    apply = self.apply_var.get()
                    if apply and not self.host_var.get().strip():
                        raise ValueError("Enter the desk's IP address on the Feedback tab first.")
                    d = Dm7Desk(self.host_var.get().strip(), "InCh:0", apply=apply, log=self._ctl_log_line)
                    if apply:
                        d.check_link()
                    self.ctl_owned = d
                self.ctl = ctlmod.Dm7Control(d)
            else:
                d, owned = self._routing_desk()
                d.log = self._ctl_log_line
                if owned:
                    self.ctl_owned = d
                self.ctl = ctlmod.QuControl(d)
        except Exception as e:
            messagebox.showerror(APP, f"Could not connect: {e}")
            return
        live = getattr(self.ctl.desk, "apply", False)
        self.ctl_status.configure(text=("Connected: changes go to the desk." if live else
                                        "Dry run: turn on Write to the desk (Feedback tab) to send changes."))
        self._ctl_targets()

    def ctl_read(self):
        if self.ctl is None or not getattr(self.ctl.desk, "apply", False) or self.ctl.name != "qu":
            messagebox.showinfo(APP, "Connect to a Qu with Write to the desk on first. Reading back works on the Qu only.")
            return
        self.ctl_status.configure(text="Reading from the desk...")

        def work():
            try:
                getattr(self.ctl.desk, "parent", self.ctl.desk).check_link()
                time.sleep(2.5)
                self.q.put(("call", lambda: (self._ctl_select(), self.ctl_status.configure(text="Read from the desk."))))
            except Exception as e:
                self.q.put(("call", lambda: self.ctl_status.configure(text=f"Read failed: {e}")))
        threading.Thread(target=work, daemon=True).start()

    def _fader_db(self):
        db = self.ctlv["fader"].get()
        return -90.0 if db <= -59.5 else db

    def _band(self, b):
        from feedback import control as ctlmod
        v = self.ctlv
        return v[f"g{b}"].get(), ctlmod.freq_from_slider(v[f"f{b}"].get()), ctlmod.width_from_slider(v[f"w{b}"].get())

    def _ctl_hpf_fn(self, c, t):
        c.hpf(t, self.ctlv["hpf_in"].get(), self.ctlv["hpf_hz"].get())

    def _ctl_phantom(self):
        on = self.ctlv["phantom"].get()
        if on and not self.ctlv["mute"].get() and not messagebox.askyesno(
                APP, "Switching on 48V makes a pop. Turn it on anyway (the channel isn't muted)?"):
            self.ctlv["phantom"].set(False)
            return
        self._ctl("phantom", lambda c, t: c.phantom(t, on))

    def _ctl_flat(self):
        c, t = self._ctl_control(), self.ctl_target.get()
        geq = c.has_geq(t)
        if not messagebox.askyesno(APP, f"Flatten the EQ on {t}? All PEQ gains go to 0 dB"
                                        f"{' and every GEQ fader goes to 0 dB' if geq else ''}."):
            return
        if geq:
            self._ctl("geq_flat", lambda c2, t2: c2.geq_flat(t2))
        for b in (1, 2, 3, 4):
            self.ctlv[f"g{b}"].set(0.0)
            self._ctl(f"band{b}", lambda c, t, b=b: c.peq_band(t, b, *self._band(b)))

    def _ctl_labels(self):
        v = self.ctlv
        db = self._fader_db()
        self.ctl_fader_lbl.configure(text="-inf" if db <= -90 else f"{db:+.1f} dB")
        self.ctl_gain_lbl.configure(text=f"{v['gain'].get():+.0f} dB")
        self.ctl_hpf_lbl.configure(text=f"{v['hpf_hz'].get():.0f} Hz")
        for b in (1, 2, 3, 4):
            g, f, w = self._band(b)
            self.ctl_band_lbl[b].configure(text=f"{g:+.1f} dB  {f:,.0f} Hz  {w:.2f} oct")
        for key, (on, lv, post, lbl) in self.ctl_send_vars.items():
            d = lv.get()
            lbl.configure(text="-inf" if d <= -59.5 else f"{d:+.0f}")

    def _ctl(self, key, fn):
        """Send a change. Slider moves are batched: the newest value goes out 60 ms later."""
        self._ctl_labels()
        if self.ctl_loading:
            return
        t = self.ctl_target.get()
        old = self.ctl_pending.pop(key, None)
        if old:
            self.root.after_cancel(old)

        def go():
            self.ctl_pending.pop(key, None)
            try:
                fn(self._ctl_control(), t)
            except Exception as e:
                self.log(f"[control] {type(e).__name__}: {e}")
        self.ctl_pending[key] = self.root.after(60, go)

    def _ctl_select(self):
        """Channel changed (or read back): rebuild the sends and load what the desk reported."""
        c = self._ctl_control()
        t = self.ctl_target.get()
        is_in = c.is_input(t)
        for w in (self.ctl_in,):
            for child in w.winfo_children():
                try:
                    child.state(["!disabled"] if c.is_mono_input(t) else ["disabled"])
                except (tk.TclError, AttributeError):
                    pass
        for w in (self.ctl_hpf_sw, self.ctl_hpf_scale):
            w.state(["!disabled"] if is_in else ["disabled"])
        # Sends
        for child in self.ctl_sends.winfo_children():
            child.destroy()
        self.ctl_send_vars = {}
        got = c.read(t)
        if is_in:
            rows = [(m, t, m) for m in c.mixes()]
        elif c.is_mix(t):
            rows = [(src, src, t) for src in c.targets() if c.is_input(src)]
        else:
            rows = []
            ttk.Label(self.ctl_sends, text="L+R has no sends. Pick an input or a mix.", style="Muted.TLabel").grid(row=0, column=0)
        cols = 2 if len(rows) > 10 else 1
        per = (len(rows) + cols - 1) // cols
        for i, (label, src, mix) in enumerate(rows):
            r, cbase = i % per, (i // per) * 4
            on = tk.BooleanVar(value=False)
            lv = tk.DoubleVar(value=-60.0)
            post = tk.BooleanVar(value=True)
            sv = self.ctl_read_send(src, mix)
            if sv:
                on.set(sv[0]); lv.set(max(-60.0, sv[1])); post.set(sv[2])
            send = lambda c2, t2, src=src, mix=mix, on=on, lv=lv, post=post: c2.send(
                src, mix, on.get(), -90.0 if lv.get() <= -59.5 else lv.get(), post.get())
            key = f"send:{src}>{mix}"
            self._tip(ttk.Checkbutton(self.ctl_sends, text=label, variable=on,
                                      command=lambda k=key, f=send: self._ctl(k, f)),
                      f"Send {src} to {mix} on or off.").grid(row=r, column=cbase, sticky="w")
            self._tip(ttk.Scale(self.ctl_sends, from_=-60, to=10, variable=lv,
                                command=lambda e, k=key, f=send: self._ctl(k, f)),
                      f"Send level from {src} to {mix} (left is -inf, 0 dB is unity).").grid(row=r, column=cbase + 1, sticky="we", padx=4)
            lbl = ttk.Label(self.ctl_sends, text="", width=5)
            lbl.grid(row=r, column=cbase + 2, sticky="w")
            self._tip(ttk.Checkbutton(self.ctl_sends, text="post", variable=post,
                                      command=lambda k=key, f=send: self._ctl(k, f)),
                      "On: post-fader (the channel fader affects the send). Off: pre-fader, for monitor mixes.").grid(
                row=r, column=cbase + 3, sticky="w", padx=(0, 10))
            self.ctl_send_vars[key] = (on, lv, post, lbl)
            self.ctl_sends.columnconfigure(cbase + 1, weight=1)
        # Load the channel's own settings without sending them back to the desk.
        self.ctl_loading = True
        try:
            v = self.ctlv
            from feedback import control as ctlmod
            for k in ("fader", "pan", "gain", "hpf_hz"):
                if k in got:
                    v[k].set(max(-60.0, got[k]) if k == "fader" else got[k])
            for k in ("mute", "phantom", "peq_in", "hpf_in", "lr"):
                if k in got:
                    v[k].set(got[k])
            for b in (1, 2, 3, 4):
                if f"band{b}" in got:
                    g, f, w = got[f"band{b}"]
                    v[f"g{b}"].set(g); v[f"f{b}"].set(ctlmod.slider_from_freq(f)); v[f"w{b}"].set(ctlmod.slider_from_width(w))
            self._ctl_labels()
        finally:
            self.ctl_loading = False

    def ctl_read_send(self, src, mix):
        return self._ctl_control().read(src).get(f"send:{mix}")

    def _routing_desk(self):
        """Returns (desk, owned). Reuses the running connection (the Qu takes only one client)."""
        kind = DESKS[self.desk_var.get()]
        if kind not in ("qu", "qu-geq", "qu-split", "dry"):
            raise ValueError("The routing page is for the Qu. Pick a Qu desk on the Feedback tab.")
        if self.desk is not None and isinstance(self.desk, QuDesk):
            import copy
            v = copy.copy(self.desk)          # same connection, but a plain Qu channel view
            v.__class__, v._owner = QuDesk, False
            v.parent = getattr(self.desk, "parent", None) or self.desk
            v.log = self._routing_log
            return v, False
        apply = self.apply_var.get() and kind != "dry"
        if apply and not self.host_var.get().strip():
            raise ValueError("Enter the desk's IP address on the Feedback tab first.")
        d = QuDesk(self.host_var.get().strip(), "Ip1", midi_channel=self.midi_var.get(), apply=apply,
                   log=self._routing_log)
        if apply:
            d.check_link()
        return d, True

    def _routing_log(self, m):
        self.q.put(("log", m))
        self.q.put(("call", lambda: self._rt_print(m)))

    def _rt_print(self, m):
        self.rt_log.configure(state="normal")
        self.rt_log.insert("end", time.strftime("%H:%M:%S  ") + m + "\n")
        self.rt_log.see("end")
        self.rt_log.configure(state="disabled")

    def _routing_inputs(self, desk):
        """Input names on this desk: a Qu-16 has 16 mono inputs, the Qu-24 24, the Qu-32 32."""
        box = str(getattr(getattr(desk, "parent", None) or desk, "box", "") or "")
        n = 16 if "Qu-16" in box else 24 if "Qu-24" in box else 32
        return qu_routing.input_names(n)

    def _routing_picked(self):
        picked = list(self.rt_tree.selection())
        if not picked:
            raise ValueError("Pick at least one channel in the list.")
        return picked

    def _routing_done(self, desk, owned, msg):
        self.rt_status.configure(text=msg)
        self.log(f"[routing] {msg}")
        if owned:
            self.root.after(800, desk.close)

    def routing_read(self):
        try:
            desk, owned = self._routing_desk()
            if not desk.apply:
                raise ValueError("Turn on Write to the desk (Feedback tab) so the app can talk to the desk.")
        except Exception as e:
            messagebox.showerror(APP, str(e))
            return
        self.rt_status.configure(text="Reading from the desk...")

        def work():
            try:
                desk.check_link()
                time.sleep(2.5)  # the desk streams every parameter after the state reply
                self.q.put(("call", lambda: self._show_routing(desk, owned)))
            except Exception as e:
                self.q.put(("call", lambda: self._routing_done(desk, owned, f"Read failed: {e}")))
        threading.Thread(target=work, daemon=True).start()

    def _show_routing(self, desk, owned):
        p = desk.params
        n = 0
        for name in qu_routing.input_names(32):
            from feedback.desks import qu_channel_code
            ch = qu_channel_code(name)
            lr = p.get(ch, qu_routing.ID_LR)
            sends = []
            for m, vx in qu_routing.MIX_VX.items():
                if p.get(ch, qu_routing.ID_ASSIGN, vx):
                    pre = p.get(ch, qu_routing.ID_PREPOST, vx)
                    sends.append(m + (" (pre)" if pre else " (post)" if pre is not None else ""))
            if lr is not None:
                n += 1
            self.rt_tree.item(name, values=(name, "?" if lr is None else ("yes" if lr else "no"), ", ".join(sends)))
        self.rt_lr_vx = p.lr_vx()
        self._routing_done(desk, owned, f"Read {n} channels from the desk." if n else
                           "The desk didn't report any routing. Check Write to the desk and the IP.")

    def routing_apply(self):
        try:
            picked = self._routing_picked()
            ret = self.rt_ret.get()
            if ret in picked:
                raise ValueError(f"{ret} is the return input, so it can't also be sent to the mix (that makes a loop).")
            mix = self.rt_mix.get()
            live = self.apply_var.get() and DESKS[self.desk_var.get()] != "dry"
            if live and not messagebox.askyesno(APP, f"Send {', '.join(picked)} to {mix} "
                                                f"({'post' if self.rt_post.get() else 'pre'}-fade)"
                                                f"{' and take them off L+R' if self.rt_offlr.get() else ''}?"):
                return
            desk, owned = self._routing_desk()
            vx = getattr(self, "rt_lr_vx", 0x07)
            others = [n for n in self._routing_inputs(desk) if n not in picked] if self.rt_clear.get() else []
            self._rt_sent = []
            qu_routing.route_to_mix(desk, picked, mix, float(self.rt_send.get()), self.rt_post.get(),
                                    self.rt_offlr.get(), lr_vx=vx, others=others)
            if self.rt_mixfader.get():
                desk.view(mix if mix in ("Mix1", "Mix2", "Mix3", "Mix4") else hex(qu_routing.MIX_CODES[mix])).set_fader(0.0)
            if ret != "(none)":
                qu_routing.set_return(desk, ret, True, vx, float(self.rt_retdb.get()))
        except Exception as e:
            messagebox.showerror(APP, f"{type(e).__name__}: {e}")
            return
        for name in picked:
            vals = self.rt_tree.item(name, "values")
            self.rt_tree.item(name, values=(name, "no" if self.rt_offlr.get() else vals[1],
                                            f"{mix} ({'post' if self.rt_post.get() else 'pre'})"))
        if self.rt_clear.get():
            for name in self.rt_tree.get_children():
                if name not in picked:
                    vals = self.rt_tree.item(name, "values")
                    self.rt_tree.item(name, values=(name, vals[1], ", ".join(
                        x for x in str(vals[2]).split(", ") if x and not x.startswith(mix + " ") and x != mix)))
        self._routing_done(desk, owned, f"{'Sent' if desk.apply else 'Dry run: would send'} {', '.join(picked)} to {mix}"
                           + (f", back on {ret}" if ret != "(none)" else "") + ". Check the desk's routing screen.")

    def routing_undo(self):
        try:
            picked = self._routing_picked()
            desk, owned = self._routing_desk()
            qu_routing.restore_to_lr(desk, picked, self.rt_mix.get(), getattr(self, "rt_lr_vx", 0x07))
        except Exception as e:
            messagebox.showerror(APP, f"{type(e).__name__}: {e}")
            return
        for name in picked:
            self.rt_tree.item(name, values=(name, "yes", ""))
        self._routing_done(desk, owned, f"{', '.join(picked)} back on L+R and off {self.rt_mix.get()}.")

    def routing_watch(self):
        mix = self.rt_mix.get()
        target = mix if mix in ("Mix1", "Mix2", "Mix3", "Mix4") else hex(qu_routing.MIX_CODES[mix])
        self.target_var.set(target)
        self.nb.select(0)
        self.rt_status.configure(text=f"Feedback tab: target set to {target}. Set Input ch and press Add / update.")

    # ------------------------------------------------------------- analyser
    RTA_LO, RTA_HI, RTA_TOP, RTA_BOT = 20.0, 20000.0, 0.0, -90.0
    EQ_TOP, EQ_BOT = 6.0, -18.0

    def _build_rta(self, tab):
        T = self._tip
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Show channel").pack(side="left")
        self.rta_pick = tk.StringVar()
        self.rta_box = T(ttk.Combobox(bar, textvariable=self.rta_pick, state="readonly", width=16),
                         "Which watched channel to show. The list is the rows on the Feedback tab.")
        self.rta_box.pack(side="left", padx=(8, 18))
        self.rta_hold = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(bar, text="Peak hold", variable=self.rta_hold, style="Switch.TCheckbutton"),
          "Keep a thin line at the highest level each frequency reached, falling back slowly.").pack(side="left")
        self.rta_eq = tk.BooleanVar(value=True)
        T(ttk.Checkbutton(bar, text="Show cuts", variable=self.rta_eq, style="Switch.TCheckbutton"),
          "Draw the EQ the app has put on the desk for this channel (the green curve, scale on the right).").pack(side="left", padx=16)
        self.rta_pause = tk.BooleanVar(value=False)
        T(ttk.Checkbutton(bar, text="Pause", variable=self.rta_pause, style="Switch.TCheckbutton"),
          "Freeze the picture so you can read it. Listening and cutting carry on as normal.").pack(side="left")
        legend = ttk.Label(bar, style="Muted.TLabel",
                           text="Blue: level   Orange: ringing   Red: feedback   Green: cuts")
        legend.pack(side="right", padx=(16, 0))
        self.rta = tk.Canvas(tab, background="#101418", highlightthickness=0)
        self.rta.pack(fill="both", expand=True)
        T(self.rta, "Live analyser of the channel's audio. Left to right is 20 Hz to 20 kHz.\n"
                    "Orange dots are peaks that look like they're starting to ring.\n"
                    "Red markers are feedback the app has found (they fade after 10 s).\n"
                    "The green curve is the EQ cut on the desk; its scale is on the right.")
        self.rta_centres = rta.log_centres()
        self.rta_holdv = None
        self.rta.bind("<Configure>", lambda e: self._draw_rta(force=True))
        self.root.after(80, self._rta_tick)

    # ------------------------------------------------------------ meters
    METER_MAX = 64          # most audio channels shown (Qu USB has 18, DM7 Dante up to 144)
    METER_LO = -60.0

    def _build_meters(self, tab):
        T = self._tip
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="Name channels as").pack(side="left")
        self.meter_names = tk.StringVar(value="Qu-16 USB")
        T(ttk.Combobox(bar, textvariable=self.meter_names, values=["Qu-16 USB", "DM7 / Dante", "Numbers"],
                       state="readonly", width=14),
          "Qu-16 USB: channels 1-16 are the inputs, 17-18 the main L/R (as set in Setup > Audio > USB).\n"
          "DM7 / Dante: channels are numbered as patched in Dante Controller.\n"
          "Numbers: just the channel number on the audio device.").pack(side="left", padx=(8, 16))
        self.meter_btn = T(ttk.Button(bar, text="Start meters", command=self.toggle_meters),
                           "Show the levels of every channel on the audio input chosen on the Feedback tab,\n"
                           "without starting the suppressor. While the suppressor runs, the meters use its feed.")
        self.meter_btn.pack(side="left")
        ttk.Label(bar, style="Muted.TLabel",
                  text="Highlighted: channels the app watches   Red top: clipping   Red name: feedback found").pack(side="right")
        self.meter_cv = tk.Canvas(tab, background="#101418", highlightthickness=0)
        self.meter_cv.pack(fill="both", expand=True)
        T(self.meter_cv, "Peak level of each channel coming from the desk, in dBFS, with a peak-hold line.\n"
                         "Aim for speech peaks around -18 to -10 dBFS; the top of the meter is 0 dBFS (digital full scale).")
        self.meter_disp = None
        self.meter_hold = None
        self.meter_clip = {}
        self.root.after(60, self._meter_tick)

    def _stop_meter_stream(self):
        if self.meter_stream is not None:
            try:
                self.meter_stream.close()
            except Exception:
                pass
            self.meter_stream = None
            if hasattr(self, "meter_btn"):
                self.meter_btn.configure(text="Start meters")

    def toggle_meters(self):
        if self.meter_stream is not None:
            self._stop_meter_stream()
            return
        if self.running:
            self.log("[meters] The suppressor is running; the meters already use its feed.")
            return
        if not self.dev_var.get():
            messagebox.showerror(APP, "Pick an audio input on the Feedback tab first.")
            return
        try:
            import sounddevice as sd
            dev_index = int(self.dev_var.get().split(":", 1)[0])
            info = dict(self.devices)[dev_index]
            n = max(1, min(int(info["max_input_channels"]), self.METER_MAX))

            def cb(indata, frames, t, status):
                self.meter_lv = 20 * np.log10(np.abs(indata).max(axis=0).astype(np.float64) + 1e-9)

            self.meter_stream = sd.InputStream(device=dev_index, channels=n, samplerate=int(info["default_samplerate"]) or 48000,
                                               blocksize=1024, dtype="float32", callback=cb)
            self.meter_stream.start()
            self.meter_btn.configure(text="Stop meters")
            self.log(f"[meters] Showing {n} channel(s) of {info['name']}")
        except Exception as e:
            self.meter_stream = None
            messagebox.showerror(APP, f"Could not open the audio input: {e}")

    def _meter_name(self, i, n):
        mode = self.meter_names.get()
        if mode == "Qu-16 USB" and n >= 18:
            return str(i + 1) if i < 16 else ("L", "R")[i - 16] if i < 18 else str(i + 1)
        return str(i + 1)

    def _meter_tick(self):
        try:
            if self.nb.index(self.nb.select()) == 2:
                self._draw_meters()
        except Exception as e:  # never let the display stop the app
            self.log(f"[meters] {type(e).__name__}: {e}")
        self.root.after(60, self._meter_tick)

    def _meter_y(self, db, top, bot):
        db = min(0.0, max(self.METER_LO, db))
        return top + (bot - top) * (db / self.METER_LO)

    def _draw_meters(self):
        cv = self.meter_cv
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 200 or h < 120:
            return
        cv.delete("all")
        text, grid = "#7d8a96", "#26303a"
        lv = self.meter_lv
        if lv is None or not (self.running or self.meter_stream is not None):
            cv.create_text(w / 2, h / 2, fill=text, font=("Segoe UI", 12),
                           text="Press Start meters (or Start on the Feedback tab) to see every channel's level.")
            return
        lv = np.asarray(lv, dtype=float)
        n = len(lv)
        # Ballistics: instant rise, 20 dB/s fall; peak hold falls after 1.5 s.
        now = time.monotonic()
        if self.meter_disp is None or len(self.meter_disp) != n:
            self.meter_disp, self.meter_hold = lv.copy(), [(v, now) for v in lv]
        self.meter_disp = np.maximum(lv, self.meter_disp - 1.2)
        for i, v in enumerate(lv):
            hv, ht = self.meter_hold[i]
            if v >= hv:
                self.meter_hold[i] = (v, now)
            elif now - ht > 1.5:                      # held for 1.5 s, then falls
                self.meter_hold[i] = (max(v, hv - 1.0), ht)
            if v >= -1.0:
                self.meter_clip[i] = now
        top, bot, left = 14, h - 44, 40
        for db in (0, -6, -12, -18, -24, -36, -48, -60):
            y = self._meter_y(db, top, bot)
            cv.create_line(left - 4, y, w - 8, y, fill=grid)
            cv.create_text(left - 18, y, text=f"{db}", fill=text, font=("Segoe UI", 8))
        watched = {}
        for r in self.rows:
            watched.setdefault(r["chan"] - 1, []).append(r)
        slot = (w - left - 10) / n
        bw = max(3.0, min(36.0, slot * 0.7))
        for i in range(n):
            x0 = left + i * slot + (slot - bw) / 2
            x1 = x0 + bw
            if i in watched:
                cv.create_rectangle(x0 - 3, top - 4, x1 + 3, bot + 2, outline="#4aa3ff", width=1)
            cv.create_rectangle(x0, top, x1, bot, fill="#1a2128", outline="")
            v = self.meter_disp[i]
            if v > self.METER_LO:
                # green below -18, yellow to -6, red above
                for lo, hi, col in ((self.METER_LO, -18.0, "#3ddc84"), (-18.0, -6.0, "#ffd23f"), (-6.0, 0.0, "#ff4d4d")):
                    if v > lo:
                        cv.create_rectangle(x0, self._meter_y(min(v, hi), top, bot), x1, self._meter_y(lo, top, bot),
                                            fill=col, outline="")
            hv = self.meter_hold[i][0]
            if hv > self.METER_LO:
                y = self._meter_y(hv, top, bot)
                cv.create_line(x0, y, x1, y, fill="#e8eef4", width=2)
            if now - self.meter_clip.get(i, -99) < 2.0:
                cv.create_rectangle(x0, top - 10, x1, top - 3, fill="#ff2020", outline="")
            howl = any(now - t < 3 for r in watched.get(i, []) for t, *_ in r.get("howls", []))
            name = self._meter_name(i, n)
            step = 1 if slot >= 18 else (2 if slot >= 9 else 4)
            if i in watched or i % step == 0:
                cv.create_text((x0 + x1) / 2, bot + 12, text=name, font=("Segoe UI", 8, "bold" if i in watched else ""),
                               fill="#ff4d4d" if howl else ("#4aa3ff" if i in watched else text))
            if i in watched and slot >= 24:
                cv.create_text((x0 + x1) / 2, bot + 26, text=watched[i][0]["target"], font=("Segoe UI", 7),
                               fill="#ff4d4d" if howl else "#4aa3ff")

    def _rta_x(self, f, w):
        return 50 + (w - 100) * math.log(f / self.RTA_LO) / math.log(self.RTA_HI / self.RTA_LO)

    def _rta_y(self, db, h):
        db = min(self.RTA_TOP, max(self.RTA_BOT, db))
        return 12 + (h - 40) * (self.RTA_TOP - db) / (self.RTA_TOP - self.RTA_BOT)

    def _eq_y(self, db, h):
        db = min(self.EQ_TOP, max(self.EQ_BOT, db))
        return 12 + (h - 40) * (self.EQ_TOP - db) / (self.EQ_TOP - self.EQ_BOT)

    def _rta_row(self):
        rows = [r for r in self.rows]
        names = [f"in {r['chan']} -> {r['target']}" for r in rows]
        if list(self.rta_box["values"]) != names:
            self.rta_box["values"] = names
        if names and self.rta_pick.get() not in names:
            self.rta_pick.set(names[0])
        if self.rta_pick.get() in names:
            return rows[names.index(self.rta_pick.get())]
        return None

    def _rta_tick(self):
        try:
            if self.nb.index(self.nb.select()) == 1 and not self.rta_pause.get():
                self._draw_rta()
        except Exception as e:  # never let the display stop the app
            self.log(f"[analyser] {type(e).__name__}: {e}")
        self.root.after(80, self._rta_tick)

    def _draw_rta(self, force=False):
        cv = self.rta
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 200 or h < 120:
            return
        cv.delete("all")
        grid, text = "#26303a", "#7d8a96"
        for f in (31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000):
            x = self._rta_x(f, w)
            cv.create_line(x, 12, x, h - 28, fill=grid)
            cv.create_text(x, h - 16, text=f"{f / 1000:g}k" if f >= 1000 else f"{f:g}", fill=text, font=("Segoe UI", 8))
        for db in range(0, -91, -10):
            y = self._rta_y(db, h)
            cv.create_line(50, y, w - 50, y, fill=grid)
            cv.create_text(28, y, text=f"{db}", fill=text, font=("Segoe UI", 8))
        cv.create_text(28, h - 16, text="dBFS", fill=text, font=("Segoe UI", 8))
        r = self._rta_row()
        sup = r.get("sup") if r else None
        det = sup.detector if sup else None
        if det is None or det.last_pdb is None:
            cv.create_text(w / 2, h / 2, fill=text, font=("Segoe UI", 12),
                           text="Press Start on the Feedback tab to see the live spectrum here.")
            return
        c = self.rta_centres
        lv = rta.band_levels(det.last_pdb, det.bin_hz, c)
        xs = [self._rta_x(f, w) for f in c]
        pts = [(xs[0], h - 28)] + [(x, self._rta_y(v, h)) for x, v in zip(xs, lv)] + [(xs[-1], h - 28)]
        cv.create_polygon(*[p for xy in pts for p in xy], fill="#1d4f7a", outline="")
        cv.create_line(*[p for xy in pts[1:-1] for p in xy], fill="#4aa3ff", width=2)
        if self.rta_hold.get():
            if self.rta_holdv is None or len(self.rta_holdv) != len(lv):
                self.rta_holdv = lv.copy()
            self.rta_holdv = np.maximum(lv, self.rta_holdv - 0.6)
            cv.create_line(*[p for x, v in zip(xs, self.rta_holdv) for p in (x, self._rta_y(v, h))], fill="#c8d6e5")
        # Peaks that are starting to ring.
        for f, v in det.last_seen:
            if self.RTA_LO <= f <= self.RTA_HI:
                x, y = self._rta_x(f, w), self._rta_y(v, h)
                cv.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#ff9f1a", outline="")
                cv.create_text(x, y - 14, text=f"{f:.0f}", fill="#ff9f1a", font=("Segoe UI", 8, "bold"))
        # Feedback the app found, fading over 10 s.
        now = time.monotonic()
        latest = {}  # one marker per frequency (the newest), so labels don't pile up
        for t, f, action in r.get("howls", []):
            latest[round(math.log2(f) * 24)] = (t, f, action)
        for i, (t, f, action) in enumerate(sorted(latest.values(), key=lambda h: h[1])):
            age = now - t
            if age > 10 or not self.RTA_LO <= f <= self.RTA_HI:
                continue
            x = self._rta_x(f, w)
            col = "#ff4d4d" if age < 3 else "#a33a3a"
            cv.create_line(x, 12, x, h - 28, fill=col, dash=(4, 3), width=2 if age < 3 else 1)
            cv.create_text(x + 4, 22 + 14 * (i % 3), text=f"{f:.0f} Hz {action}", fill=col, anchor="w",
                           font=("Segoe UI", 9, "bold"))
        # The cuts on the desk, on the right-hand scale.
        notches = list(sup.manager.notches.values())
        if self.rta_eq.get():
            for db in (0, -6, -12, -18):
                cv.create_text(w - 26, self._eq_y(db, h), text=f"{db:+d}", fill="#3ddc84", font=("Segoe UI", 8))
            y0 = self._eq_y(0, h)
            cv.create_line(50, y0, w - 50, y0, fill="#1f5c3a", dash=(2, 4))
            if notches:
                fx = rta.log_centres(per_octave=48)
                eq = rta.eq_response_db(notches, fx, QU_GEQ_FREQS if self.mode in ("qu-geq", "qu-split") else None,
                                        split=self.mode == "qu-split")
                cv.create_line(*[p for f, g in zip(fx, eq) for p in (self._rta_x(f, w), self._eq_y(g, h))],
                               fill="#3ddc84", width=3, smooth=True)
                for n in notches:
                    x = self._rta_x(min(self.RTA_HI, max(self.RTA_LO, n.freq_hz)), w)
                    if n.band == 0:
                        cv.create_text(x, self._eq_y(-3, h) - 12, fill="#3ddc84", font=("Segoe UI", 8),
                                       text=f"HPF {n.freq_hz:.0f} Hz")
                        continue
                    cv.create_text(x, self._eq_y(n.gain_db, h) + 14, fill="#3ddc84", font=("Segoe UI", 8),
                                   text=f"{n.freq_hz:.0f} Hz {n.gain_db:+.0f} dB")

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
            if n.band == 0:
                out.append(f"HPF {n.freq_hz:.0f}Hz")
            elif self.mode == "qu-geq" or n.band >= GEQ_OFFSET:
                out.append(f"GEQ {n.freq_hz:.0f}Hz {n.gain_db:+.0f}")
            else:
                name = QU_BAND_NAMES.get(n.band, n.band) if self.mode in ("qu", "qu-split") else f"B{n.band}"
                out.append(f"{name} {n.freq_hz:.0f}Hz {n.gain_db:+.0f}dB {n.width_oct:.2f}oct")
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
        if kind == "qu-split":
            return QuSplitDesk(host, target, midi_channel=self.midi_var.get(), apply=apply, log=log)
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
                if kind in ("qu", "qu-split", "dm7"):
                    (Dm7Desk if kind == "dm7" else QuDesk).check_bands(r["band_list"])
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
            speed = self.speed_var.get() if self.speed_var.get() in SPEEDS else "normal"
            views, managers = {}, {}
            for r in self.rows:
                # Rows that point at the same desk channel share one set of bands, so they
                # can never fight over a band and make it jump between frequencies.
                key = r["target"].lower()
                if key not in views:
                    views[key] = self.desk.view(r["target"])
                    use_hpf = self.hpf_var.get() and getattr(views[key], "has_hpf", False) and kind not in ("qu-geq", "qu-split")
                    ncfg = NotchConfig(bands=r["band_list"], max_depth_db=MAX_DEPTH[kind],
                                       hpf_max_hz=float(self.hpfmax_var.get()) if use_hpf else 0.0,
                                       release_after_s=8.0 if self.recover_var.get() else 0.0,
                                       total_cut_db=24.0 if kind == "qu-geq" else 18.0,
                                       cooldown_s=SPEEDS[speed]["cooldown_s"])
                    if kind == "qu-split":
                        # PEQ for the severe, narrow howls; GEQ for the gentler, broader ones.
                        ncfg.total_cut_db = 15.0
                        gcfg = NotchConfig(max_depth_db=MAX_DEPTH[kind], total_cut_db=18.0,
                                           release_after_s=ncfg.release_after_s, cooldown_s=ncfg.cooldown_s)
                        managers[key] = SplitManager(NotchManager(ncfg), GeqManager(QU_GEQ_FREQS, gcfg))
                    elif kind == "qu-geq":
                        managers[key] = GeqManager(QU_GEQ_FREQS, ncfg)
                    else:
                        managers[key] = NotchManager(ncfg)
                r["desk"], manager = views[key], managers[key]
                ncfg = manager.cfg
                r["howls"] = []
                r["sup"] = Suppressor(r["desk"], speed_config(speed, sr, self.sens_var.get(),
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

            self._stop_meter_stream()
            nch = max(nch, min(int(info["max_input_channels"]), self.METER_MAX))  # all channels, for the meters
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
        row.setdefault("howls", []).append((time.monotonic(), ev.freq_hz, action))
        del row["howls"][:-20]
        if self.ringout and self.ro_row is row:
            self.ringout.on_howl(action, ev.freq_hz)

    def _worker(self):
        while self.running:
            try:
                x = self.blocks.get(timeout=0.2)
            except queue.Empty:
                continue
            self.meter_lv = 20 * np.log10(np.abs(x).max(axis=0) + 1e-9)
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

    def _apply_recover(self):
        for r in self.rows:
            if "sup" in r:
                m = r["sup"].manager
                for sub in getattr(m, "managers", [m]):
                    sub.cfg.release_after_s = 8.0 if self.recover_var.get() else 0.0

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
                elif kind == "call":
                    val()
        except queue.Empty:
            pass
        if self.running or self.ringout:
            self._show_notches()
        self.root.after(150, self._pump)

    def _save_settings(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        data = {k: getattr(self, k + "_var").get() for k in
                ("dev", "chan", "desk", "host", "target", "bands", "midi", "sens", "floor", "hpf", "hpfmax", "recover", "speed")}
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
        self._stop_meter_stream()
        if self.ctl_owned is not None:
            self.ctl_owned.close()
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
    def try_routing():
        app.rt_tree.selection_set(("Ip3", "Ip4"))
        app.rt_ret.set("Ip16")
        app.routing_apply()
    root.after(1000, try_routing)
    def try_control():
        app.ctl_target.set("Mix1")
        app._ctl_select()
        app.ctlv["fader"].set(-5.0)
        app._ctl("fader", lambda c, t: c.fader(t, app._fader_db()))
        app._ctl_flat()
        app.ctl_target.set("Ip2")
        app._ctl_select()
    root.after(1500, try_control)
    root.after(3500, lambda: app.nb.select(2))  # CI screenshots: feedback tab, then the meters
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
            problems = [w for w in ("Started: in 1 -> Ip1, in 2 -> Mix1", "[ring-out] Mix1", "Saved cuts",
                                    "[routing] Dry run: would send Ip3, Ip4 to Mix1",
                                    "Mix1 fader -5.0 dB", "GEQ flat")
                        if w not in text]
            if "error:" in text:
                problems.append("an error was logged")
            if "[analyser]" in text:
                problems.append("the analyser failed")
            if "[control]" in text:
                problems.append("the control page failed")
            if "[meters]" in text:
                problems.append("the meters failed")
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
