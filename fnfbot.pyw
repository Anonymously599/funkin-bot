"""
fnfbot.py  —  FNF Bot GUI  (Python Edition)

Bugs fixed vs previous version:
  1. _on_tick lambda closure bug — ms captured by reference, fixed with default arg
  2. _on_hit lambda closure — lane captured by ref in loop, fixed
  3. NoteCanvas._anim() keeps firing after widget destroyed — added _alive guard
  4. _redraw mutates dict while iterating (RuntimeError) — iterate copy
  5. Canvas stipple "gray50" crashes on some Windows Tcl builds — use rectangle alpha workaround
  6. Settings scroll MouseWheel binding leaks globally — bound only to settings canvas
  7. _build_settings_tab called before _key_vars/_hk_vars populated — order fixed
  8. bot_engine callbacks called from worker thread touching Tk widgets — all via after(0,)
  9. BotEngine.stop() sets playing=False before thread reads _stop — use Event properly
 10. Multiple hotkey listener threads piling up on repeated _start_hotkeys — guard flag
 11. SETTINGS_FILE path fails when run as PyInstaller exe — sys.executable fallback
 12. load_settings deep-merges sub-dicts so nested keybinds aren't wiped by partial saves
 13. Console text widget: tag "info" defined after first _log calls — moved to init
 14. Flash dict RuntimeError on concurrent modification — copy before iterate
 15. on_tick fires 60x/sec from bot thread, flooding after() queue — throttle to 30fps
 16. PyInstaller: __file__ undefined in frozen exe — use sys.executable path
 17. Offset StringVar can be empty string on clear — clamped in getter
 18. Bot started while previous bot still winding down — stop old bot first
 19. Unicode chars in title bar crash some Windows terminal encodings — safe fallback
 20. Window closes while bot is playing, holding keys forever — WM_DELETE_WINDOW handler
 21. Custom per-type note overrides — per-note-type checkboxes (built
     after chart load) let you force-click or force-skip a specific
     note type, overriding the global harmful-skip default
 22. Multi-difficulty chart support — charts with more than one
     difficulty now prompt a selection dialog before loading
 23. Note lane ownership fixed to absolute rule (lanes 0-3 = player,
     4-7 = opponent) — old mustHitSection-swap logic mis-pressed
     opponent notes on both Psych Engine and P-Slice charts
"""

import tkinter as tk
from tkinter import filedialog, messagebox
import threading
import time
import os
import sys
import traceback
import json
import copy

# ── Resolve base path (works both as .py and PyInstaller .exe) ────────
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

SETTINGS_FILE = os.path.join(_BASE, "fnfbot_settings.json")

# ── Safe module imports ───────────────────────────────────────────────
_import_errors = []
ChartParser = FNFNote = BotEngine = start_global_hotkeys = get_backend_name = None

try:
    from fnf_song import ChartParser, FNFNote
except Exception as _e:
    _import_errors.append("fnf_song: " + str(_e))

try:
    from bot_engine import BotEngine, start_global_hotkeys, get_backend_name
except Exception as _e:
    _import_errors.append("bot_engine: " + str(_e))

DEPS_OK = len(_import_errors) == 0

# ── Theme ─────────────────────────────────────────────────────────────
BG          = "#12121e"
RED_BG      = "#1a0e0e"
GREEN_BG    = "#0c180c"
BLUE_BG     = "#0c0c1a"
SETS_BG     = "#0e0e18"
ACCENT      = "#e94560"
G_ACC       = "#39d353"
B_ACC       = "#4d7cff"
TEXT        = "#e0e0f0"
DIM         = "#55556a"
SEP_COL     = "#2a2a3a"
NOTE_COLS   = ["#ff4f4f", "#ffd700", "#44ee77", "#4d9fff"]
HOLD_COL    = "#9977ee"
F_MAIN      = ("Consolas", 9)
F_BOLD      = ("Consolas", 9,  "bold")
F_BIG       = ("Consolas", 11, "bold")
F_TINY      = ("Consolas", 8)
F_MONO10    = ("Consolas", 10)

# ── Settings ──────────────────────────────────────────────────────────
_DEFAULTS = {
    "keybinds":       {"0": "left", "1": "down", "2": "up", "3": "right"},
    "hotkeys":        {"start_stop": "f1", "offset_up": "f2", "offset_down": "f3"},
    "offset":         25,
    "global_hotkeys": True,
    "fast_mode":      False,
    "skip_harmful":   True,   # skip Mine/MissNote/Void/Hurt notes (recommended)
    "custom_notes":   {},     # note_type_str -> bool (True = bot clicks it, overrides class)
}

def _deep_merge(base, override):
    """Merge override into base, recursing into sub-dicts."""
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result

def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return _deep_merge(_DEFAULTS, data)
    except Exception:
        pass
    return copy.deepcopy(_DEFAULTS)

def save_settings(s):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


# ── Note Preview Canvas ───────────────────────────────────────────────
class NoteCanvas(tk.Canvas):
    LW     = 44          # lane width px
    H      = 400         # canvas height px
    WIN_MS = 3000.0      # ms of lookahead shown

    def __init__(self, parent, **kw):
        super().__init__(parent,
                         width=self.LW * 4,
                         height=self.H,
                         bg=BLUE_BG,
                         highlightthickness=0,
                         **kw)
        self._notes    = []
        self._elapsed  = 0.0
        self._flashes  = {}   # lane -> expiry (perf_counter)
        self._alive    = True
        self._draw_base()
        self._schedule_anim()

    def destroy(self):
        self._alive = False
        super().destroy()

    def _draw_base(self):
        syms = ["\u2190", "\u2193", "\u2191", "\u2192"]   # ← ↓ ↑ →
        for i in range(4):
            x = i * self.LW
            shade = "#0d0d20" if i % 2 == 0 else "#0c0c1c"
            self.create_rectangle(x, 0, x + self.LW, self.H,
                                  fill=shade, outline="", tags="base")
            self.create_line(x, 0, x, self.H,
                             fill="#1e1e3a", tags="base")
            self.create_line(x, self.H - 34, x + self.LW, self.H - 34,
                             fill="#2a2a4a", dash=(3, 3), tags="base")
            self.create_text(x + self.LW // 2, self.H - 17,
                             text=syms[i], fill=NOTE_COLS[i],
                             font=("Consolas", 12, "bold"), tags="base")

    def set_notes(self, notes):
        self._notes   = sorted(notes, key=lambda n: n.time)
        self._elapsed = 0.0

    def update_time(self, ms):
        self._elapsed = ms

    def flash_lane(self, lane):
        self._flashes[lane] = time.perf_counter() + 0.14

    def _schedule_anim(self):
        if not self._alive:
            return
        try:
            self._redraw()
            self.after(16, self._schedule_anim)   # ~60fps
        except Exception:
            pass

    def _redraw(self):
        self.delete("dyn")
        now = self._elapsed
        end = now + self.WIN_MS
        H   = self.H
        t   = time.perf_counter()

        # Flash hit zones (copy dict to avoid RuntimeError on concurrent write)
        for lane, exp in list(self._flashes.items()):
            if t < exp:
                x     = lane * self.LW
                # Use solid color instead of stipple to avoid Windows Tcl crashes
                self.create_rectangle(x + 2, H - 34, x + self.LW - 2, H - 2,
                                      fill=NOTE_COLS[lane], outline="", tags="dyn")
            else:
                self._flashes.pop(lane, None)

        # Notes
        for note in self._notes:
            t_note = note.time
            if t_note < now - 200:
                continue
            if t_note > end:
                break
            lane = note.lane % 4
            x    = lane * self.LW
            frac = (t_note - now) / self.WIN_MS
            y    = int(H * (1.0 - frac))
            col  = NOTE_COLS[lane]

            if note.hold_length > 0:
                hf   = note.hold_length / self.WIN_MS
                yend = min(H - 38, y + int(H * hf))
                if yend > y + 4:
                    self.create_rectangle(
                        x + 15, y + 3, x + self.LW - 15, yend,
                        fill=HOLD_COL, outline="", tags="dyn")
                    self.create_oval(
                        x + 12, yend - 4, x + self.LW - 12, yend + 4,
                        fill="#bb99ff", outline="", tags="dyn")

            # Note head with rounded look
            self.create_rectangle(
                x + 3, y - 9, x + self.LW - 3, y + 3,
                fill=col, outline="#ffffff22", width=1, tags="dyn")
            # Gloss highlight
            self.create_rectangle(
                x + 5, y - 7, x + self.LW - 16, y - 2,
                fill="#ffffff55", outline="", tags="dyn")


# ── Main Application ──────────────────────────────────────────────────
class FNFBotApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FNFBot  \u2014  Python Edition")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.minsize(900, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Suppress console window on Windows
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        except Exception:
            pass

        self._song         = None
        self._bot          = None
        self._settings     = load_settings()
        self._hk_running   = False  # guard against stacking hotkey threads

        # Throttle on_tick: only update UI at ~30fps regardless of bot speed
        self._last_tick_ui = 0.0

        # Tk vars — must exist before _build_ui()
        self._offset_var       = tk.StringVar(value=str(self._settings.get("offset", 25)))
        self._key_vars         = {}   # int lane -> StringVar
        self._hk_vars          = {}   # str action -> StringVar
        self._global_hk        = tk.BooleanVar(value=bool(self._settings.get("global_hotkeys", True)))
        self._skip_harmful_v   = tk.BooleanVar(value=bool(self._settings.get("skip_harmful", True)))
        self._custom_note_vars = {}   # note_type_str -> BooleanVar (built after chart load)

        self._build_ui()

        # Startup log messages
        if DEPS_OK:
            self._log("FNFBot ready.  Input: {}".format(get_backend_name()))
            self._start_hotkeys()
        else:
            for err in _import_errors:
                self._log("[ERROR] " + err, "err")
            self._log("Run install.bat to fix missing modules.", "warn")

    # ── Window close handler ─────────────────────────────────────────
    def _on_close(self):
        """Release all held keys then exit cleanly."""
        if self._bot and self._bot.playing:
            self._bot.stop()
            time.sleep(0.1)
        self.destroy()

    # ── UI root structure ────────────────────────────────────────────
    def _build_ui(self):
        # Title bar
        bar = tk.Frame(self, bg=ACCENT, height=40)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="FNFBot  \u2014  Python Edition",
                 bg=ACCENT, fg="white", font=F_BIG).pack(side="left", padx=14, pady=8)
        self._hk_hint = tk.Label(bar, text="F1=Play  F2=+ms  F3=-ms",
                                  bg=ACCENT, fg="#ffcccc", font=F_TINY)
        self._hk_hint.pack(side="right", padx=14)

        # Tab bar
        tab_bar = tk.Frame(self, bg="#0e0e1a", height=32)
        tab_bar.pack(fill="x")
        tab_bar.pack_propagate(False)
        self._tab_btns   = {}
        self._tab_frames = {}

        for name in ("MAIN", "SETTINGS"):
            b = tk.Button(tab_bar, text=name,
                          font=F_BOLD, relief="flat", cursor="hand2",
                          bd=0, padx=18, pady=6,
                          command=lambda n=name: self._switch_tab(n))
            b.pack(side="left")
            self._tab_btns[name] = b

        # Content container
        self._content = tk.Frame(self, bg=BG)
        self._content.pack(fill="both", expand=True)

        # Build tabs — Settings FIRST so _key_vars/_hk_vars are populated
        # before _build_controls tries to reference them (they don't, but good practice)
        self._tab_frames["SETTINGS"] = self._build_settings_tab()
        self._tab_frames["MAIN"]     = self._build_main_tab()

        # Status bar
        self._status = tk.StringVar(value="Ready  \u2014  Load a chart to begin")
        tk.Label(self, textvariable=self._status,
                 bg="#09090f", fg=DIM, font=F_TINY,
                 anchor="w", padx=8, pady=3).pack(fill="x", side="bottom")

        self._switch_tab("MAIN")

    def _switch_tab(self, name):
        for f in self._tab_frames.values():
            f.pack_forget()
        self._tab_frames[name].pack(fill="both", expand=True)
        for n, b in self._tab_btns.items():
            b.config(bg=ACCENT if n == name else "#1a1a2a",
                     fg="white" if n == name else DIM)

    # ── MAIN tab ─────────────────────────────────────────────────────
    def _build_main_tab(self):
        frame = tk.Frame(self._content, bg=BG)

        left = tk.Frame(frame, bg=RED_BG, width=220,
                        highlightbackground="#3a1a1a", highlightthickness=1)
        left.pack(side="left", fill="y", padx=(5, 3), pady=5)
        left.pack_propagate(False)
        self._build_controls(left)

        mid = tk.Frame(frame, bg=GREEN_BG, width=320,
                       highlightbackground="#1a3a1a", highlightthickness=1)
        mid.pack(side="left", fill="both", expand=True, padx=(0, 3), pady=5)
        mid.pack_propagate(False)
        self._build_console(mid)

        right = tk.Frame(frame, bg=BLUE_BG,
                         highlightbackground="#1a1a3a", highlightthickness=1)
        right.pack(side="left", fill="y", padx=(0, 5), pady=5)
        self._build_preview(right)

        return frame

    def _hsep(self, p, bg=None):
        tk.Frame(p, bg=SEP_COL, height=1).pack(fill="x", padx=8, pady=5)

    def _sec(self, p, text, bg):
        tk.Label(p, text=text, bg=bg, fg=ACCENT,
                 font=F_BOLD, pady=2).pack(anchor="w", padx=10, pady=(6, 1))

    def _build_controls(self, p):
        self._sec(p, "CHART FILE", RED_BG)
        self._chart_lbl = tk.Label(p, text="No file loaded", bg=RED_BG, fg=DIM,
                                    font=F_TINY, wraplength=200, justify="left")
        self._chart_lbl.pack(anchor="w", padx=10, pady=1)
        tk.Button(p, text="Browse Chart", command=self._browse,
                  bg=ACCENT, fg="white", font=F_BOLD,
                  relief="flat", cursor="hand2", pady=5).pack(padx=10, pady=3, fill="x")

        self._hsep(p, RED_BG)
        self._sec(p, "SONG INFO", RED_BG)
        self._info_lbl = tk.Label(p, text="\u2014", bg=RED_BG, fg="#99dd99",
                                   font=F_TINY, justify="left", wraplength=200)
        self._info_lbl.pack(anchor="w", padx=10, pady=1)

        self._hsep(p, RED_BG)
        self._sec(p, "TIMING OFFSET (ms)", RED_BG)
        row = tk.Frame(p, bg=RED_BG)
        row.pack(padx=10, pady=3)
        tk.Button(row, text=" - ", command=self._dec_offset,
                  bg="#2e1010", fg="white", font=F_BIG,
                  relief="flat", cursor="hand2").pack(side="left", padx=2)
        tk.Label(row, textvariable=self._offset_var, bg=RED_BG, fg="white",
                 font=("Consolas", 14, "bold"), width=4).pack(side="left")
        tk.Button(row, text=" + ", command=self._inc_offset,
                  bg="#2e1010", fg="white", font=F_BIG,
                  relief="flat", cursor="hand2").pack(side="left", padx=2)

        self._hsep(p, RED_BG)
        self._play_btn = tk.Button(p, text="START  (F1)",
                                    command=self._toggle_play,
                                    bg="#136128", fg="white", font=F_BIG,
                                    relief="flat", cursor="hand2", pady=12)
        self._play_btn.pack(padx=10, pady=4, fill="x")

        self._time_var = tk.StringVar(value="00:00.000")
        tk.Label(p, textvariable=self._time_var, bg=RED_BG, fg=DIM,
                 font=F_MONO10).pack(pady=2)

        self._notes_var = tk.StringVar(value="Notes: \u2014")
        tk.Label(p, textvariable=self._notes_var, bg=RED_BG, fg=DIM,
                 font=F_TINY).pack(pady=1)

    def _build_console(self, p):
        tk.Label(p, text="CONSOLE", bg=GREEN_BG, fg=G_ACC,
                 font=F_BOLD, pady=5).pack(anchor="w", padx=8)

        wrap = tk.Frame(p, bg=GREEN_BG)
        wrap.pack(fill="both", expand=True, padx=5, pady=(0, 5))

        sb = tk.Scrollbar(wrap)
        sb.pack(side="right", fill="y")

        self._console = tk.Text(wrap,
                                 bg="#061006", fg=G_ACC,
                                 font=("Consolas", 8),
                                 state="disabled", relief="flat",
                                 wrap="word",
                                 yscrollcommand=sb.set,
                                 insertbackground="white",
                                 selectbackground=ACCENT)
        self._console.pack(fill="both", expand=True)
        sb.config(command=self._console.yview)

        # Define tags before any log calls
        self._console.tag_config("err",  foreground="#ff6666")
        self._console.tag_config("warn", foreground="#ffcc44")
        self._console.tag_config("hit",  foreground="#44ffaa")
        self._console.tag_config("info", foreground=G_ACC)

    def _build_preview(self, p):
        tk.Label(p, text="NOTE PREVIEW", bg=BLUE_BG, fg=B_ACC,
                 font=F_BOLD, pady=5).pack(anchor="w", padx=8)
        self._canvas = NoteCanvas(p)
        self._canvas.pack(padx=5, pady=(0, 5))

    # ── SETTINGS tab ─────────────────────────────────────────────────
    def _build_settings_tab(self):
        outer = tk.Frame(self._content, bg=SETS_BG)

        scroll_canvas = tk.Canvas(outer, bg=SETS_BG, highlightthickness=0)
        vsb = tk.Scrollbar(outer, orient="vertical", command=scroll_canvas.yview)
        scroll_canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        scroll_canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(scroll_canvas, bg=SETS_BG)
        win_id = scroll_canvas.create_window((0, 0), window=inner, anchor="nw")

        def _resize(e=None):
            scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all"))
            w = scroll_canvas.winfo_width()
            if w > 1:
                scroll_canvas.itemconfig(win_id, width=w)

        inner.bind("<Configure>", _resize)
        scroll_canvas.bind("<Configure>", _resize)

        # Mouse wheel — bind only to this canvas, not globally
        def _wheel(e):
            scroll_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        scroll_canvas.bind("<MouseWheel>", _wheel)
        inner.bind("<MouseWheel>", _wheel)

        p = {"padx": 20, "pady": 3}

        # ── Global hotkeys toggle ─────────────────────────────────────
        self._settings_section(inner, "GLOBAL HOTKEYS")
        tk.Label(inner,
                 text="Hotkeys fire even when FNFBot is minimized or behind another window.",
                 bg=SETS_BG, fg=DIM, font=F_TINY).pack(anchor="w", **p)

        chk_row = tk.Frame(inner, bg=SETS_BG)
        chk_row.pack(anchor="w", **p)
        tk.Checkbutton(chk_row,
                       text="Enable global hotkeys  (recommended)",
                       variable=self._global_hk,
                       bg=SETS_BG, fg=TEXT, selectcolor="#1a1a2a",
                       activebackground=SETS_BG, activeforeground=TEXT,
                       font=F_MAIN,
                       command=self._on_global_hk_toggle).pack(side="left")

        self._settings_sep(inner)

        # ── Hotkey bindings ───────────────────────────────────────────
        self._settings_section(inner, "HOTKEY BINDINGS")
        tk.Label(inner, text="Which keys trigger bot actions. Default: F1 / F2 / F3.",
                 bg=SETS_BG, fg=DIM, font=F_TINY).pack(anchor="w", **p)

        hk_saved = self._settings.get("hotkeys", _DEFAULTS["hotkeys"])
        hk_defs = [
            ("start_stop",  "Start / Stop bot",        "f1"),
            ("offset_up",   "Increase offset (+5 ms)", "f2"),
            ("offset_down", "Decrease offset (-5 ms)", "f3"),
        ]
        for key, label, default in hk_defs:
            v = tk.StringVar(value=hk_saved.get(key, default))
            self._hk_vars[key] = v
            self._entry_row(inner, label, v,
                            "(f1..f12, ctrl+z, etc.)", 26, p)

        self._settings_sep(inner)

        # ── Note key bindings ─────────────────────────────────────────
        self._settings_section(inner, "NOTE KEY BINDINGS")
        tk.Label(inner,
                 text="Keys the bot presses per lane. Match your FNF key settings.",
                 bg=SETS_BG, fg=DIM, font=F_TINY).pack(anchor="w", **p)

        kb_saved = self._settings.get("keybinds", _DEFAULTS["keybinds"])
        kb_defs = [
            (0, "LEFT  lane",  "left"),
            (1, "DOWN  lane",  "down"),
            (2, "UP    lane",  "up"),
            (3, "RIGHT lane",  "right"),
        ]
        for lane, label, default in kb_defs:
            v = tk.StringVar(value=kb_saved.get(str(lane), default))
            self._key_vars[lane] = v
            r = tk.Frame(inner, bg=SETS_BG)
            r.pack(anchor="w", **p)
            tk.Label(r, text=label, bg=SETS_BG, fg=NOTE_COLS[lane],
                     font=F_MAIN, width=12, anchor="w").pack(side="left")
            tk.Entry(r, textvariable=v, bg="#1a0e0e", fg="white",
                     font=F_MAIN, width=12, insertbackground="white",
                     relief="flat", bd=4).pack(side="left", padx=4)
            tk.Label(r, text="(left, right, a, d, space...)",
                     bg=SETS_BG, fg=DIM, font=F_TINY).pack(side="left", padx=4)

        self._settings_sep(inner)

        # ── Default offset ────────────────────────────────────────────
        self._settings_section(inner, "DEFAULT OFFSET")
        tk.Label(inner,
                 text="How many ms early the bot hits notes.\n"
                      "Adjust live with the hotkeys above.",
                 bg=SETS_BG, fg=DIM, font=F_TINY).pack(anchor="w", **p)

        self._settings_offset = tk.StringVar(
            value=str(self._settings.get("offset", 25)))
        self._entry_row(inner, "Offset (ms)", self._settings_offset,
                        "(default: 25)", 12, p)

        self._settings_sep(inner)

        # ── Fast mode toggle ───────────────────────────────────────────
        self._settings_section(inner, "FAST MODE")
        tk.Label(inner,
                 text="Enable for spammy songs with many rapid notes.\n"
                      "Uses minimum delay (0.1ms) for faster reaction time.",
                 bg=SETS_BG, fg=DIM, font=F_TINY).pack(anchor="w", **p)

        self._fast_mode_var = tk.BooleanVar(
            value=bool(self._settings.get("fast_mode", False)))
        chk_row = tk.Frame(inner, bg=SETS_BG)
        chk_row.pack(anchor="w", **p)
        tk.Checkbutton(chk_row,
                       text="Enable fast mode  (recommended for spam songs)",
                       variable=self._fast_mode_var,
                       bg=SETS_BG, fg=TEXT, selectcolor="#1a1a2a",
                       activebackground=SETS_BG, activeforeground=TEXT,
                       font=F_MAIN).pack(side="left")

        self._settings_sep(inner)

        # ── Note type filtering ───────────────────────────────────────
        self._settings_section(inner, "NOTE TYPE FILTERING")
        tk.Label(inner,
                 text="Some mods include special note types:\n"
                      "  Harmful  (Mine, MissNote, Void Note, Hurt Note) — damage/kill the player\n"
                      "  Opponent (Opponent 2 Sing, etc.)                — belong to a 2nd opponent\n"
                      "  Cosmetic (Alt Animation, Trail Note, No Sing)   — safe, click normally\n\n"
                      "Opponent notes are ALWAYS skipped.\n"
                      "Toggle below controls harmful notes only (global default).\n"
                      "Per-type checkboxes below (after loading a chart) override this per note type.",
                 bg=SETS_BG, fg=DIM, font=F_TINY, justify="left").pack(anchor="w", **p)

        chk_row2 = tk.Frame(inner, bg=SETS_BG)
        chk_row2.pack(anchor="w", **p)
        tk.Checkbutton(chk_row2,
                       text="Skip harmful note types  (Mine / MissNote / Void / Hurt — recommended ON)",
                       variable=self._skip_harmful_v,
                       bg=SETS_BG, fg=TEXT, selectcolor="#1a1a2a",
                       activebackground=SETS_BG, activeforeground=TEXT,
                       font=F_MAIN).pack(side="left")

        # Live readout updated after chart load
        self._note_types_lbl = tk.Label(inner,
                 text="Load a chart to see its note types.",
                 bg=SETS_BG, fg=DIM, font=F_TINY, justify="left", wraplength=660)
        self._note_types_lbl.pack(anchor="w", **p)

        self._custom_notes_frame = tk.Frame(inner, bg=SETS_BG)
        self._custom_notes_frame.pack(anchor="w", fill="x", **p)

        self._settings_sep(inner)

        # ── Save button ───────────────────────────────────────────────
        tk.Button(inner, text="Save Settings",
                  command=self._save_settings,
                  bg=G_ACC, fg="#000000", font=F_BIG,
                  relief="flat", cursor="hand2", pady=10
                  ).pack(padx=20, pady=8, fill="x")

        tk.Label(inner,
                 text="Saved to: " + SETTINGS_FILE,
                 bg=SETS_BG, fg=DIM, font=F_TINY).pack(pady=4)

        tk.Frame(inner, bg=SETS_BG, height=40).pack()
        return outer

    def _settings_section(self, p, text):
        f = tk.Frame(p, bg=SETS_BG)
        f.pack(fill="x", padx=20, pady=(14, 4))
        tk.Label(f, text=text, bg=SETS_BG, fg=ACCENT,
                 font=("Consolas", 10, "bold")).pack(side="left")

    def _settings_sep(self, p):
        tk.Frame(p, bg=SEP_COL, height=1).pack(fill="x", padx=20, pady=8)

    def _entry_row(self, p, label, var, hint, lwidth, pad):
        r = tk.Frame(p, bg=SETS_BG)
        r.pack(anchor="w", **pad)
        tk.Label(r, text=label, bg=SETS_BG, fg=TEXT,
                 font=F_MAIN, width=lwidth, anchor="w").pack(side="left")
        tk.Entry(r, textvariable=var, bg="#0e0e1a", fg="white",
                 font=F_MAIN, width=12, insertbackground="white",
                 relief="flat", bd=4).pack(side="left", padx=4)
        tk.Label(r, text=hint, bg=SETS_BG, fg=DIM, font=F_TINY).pack(side="left", padx=4)

    def _rebuild_custom_note_checks(self, found_types):
        """(Re)build one checkbox per note type found in the loaded chart.
        Default state: checked unless the type classifies as harmful/opponent,
        or unless the user already saved an explicit choice for this type."""
        for w in self._custom_notes_frame.winfo_children():
            w.destroy()
        self._custom_note_vars.clear()

        if not found_types:
            return

        from fnf_song import classify_note_type
        saved = self._settings.get("custom_notes", {})

        tk.Label(self._custom_notes_frame,
                 text="Per-type overrides (checked = bot WILL click it, even if harmful):",
                 bg=SETS_BG, fg=TEXT, font=F_TINY, justify="left").pack(anchor="w", pady=(6, 2))

        for nt in found_types:
            cls = classify_note_type(nt)
            default = saved.get(nt, cls not in ("harmful", "opponent"))
            v = tk.BooleanVar(value=bool(default))
            self._custom_note_vars[nt] = v
            row = tk.Frame(self._custom_notes_frame, bg=SETS_BG)
            row.pack(anchor="w", fill="x", pady=1)
            tk.Checkbutton(row, text="{}   [{}]".format(nt, cls),
                           variable=v, bg=SETS_BG, fg=TEXT, selectcolor="#1a1a2a",
                           activebackground=SETS_BG, activeforeground=TEXT,
                           font=F_TINY).pack(side="left")

    # ── Settings actions ──────────────────────────────────────────────
    def _on_global_hk_toggle(self):
        enabled = self._global_hk.get()
        self._settings["global_hotkeys"] = enabled
        save_settings(self._settings)
        if enabled:
            self._start_hotkeys()
            self._log("Global hotkeys ON.")
        else:
            self._log("Global hotkeys OFF  (restart app to fully unload hook).", "warn")

    def _save_settings(self):
        fallback_kb = ["left", "down", "up", "right"]
        fallback_hk = ["f1", "f2", "f3"]

        kb = {}
        for lane, v in self._key_vars.items():
            raw = v.get().strip()
            kb[str(lane)] = raw if raw else fallback_kb[lane]

        hk = {}
        for i, (k, v) in enumerate(self._hk_vars.items()):
            raw = v.get().strip()
            hk[k] = raw if raw else fallback_hk[i]

        try:
            offset = int(float(self._settings_offset.get()))
        except (ValueError, TypeError):
            offset = 25

        # ── FIX: persist per-note-type checkbox states ─────────────────
        # This was previously only happening inside _on_global_hk_toggle,
        # which never fires when you press "Save Settings". Merge current
        # checkbox states into the saved custom_notes dict here instead.
        merged_custom = dict(self._settings.get("custom_notes", {}))
        merged_custom.update({nt: v.get() for nt, v in self._custom_note_vars.items()})

        self._settings["keybinds"]       = kb
        self._settings["hotkeys"]        = hk
        self._settings["offset"]         = offset
        self._settings["global_hotkeys"] = self._global_hk.get()
        self._settings["fast_mode"]      = self._fast_mode_var.get()
        self._settings["skip_harmful"]   = self._skip_harmful_v.get()
        self._settings["custom_notes"]   = merged_custom

        save_settings(self._settings)

        # Apply immediately to main offset display
        self._offset_var.set(str(offset))
        # Apply to running bot if any
        if self._bot and self._bot.playing:
            self._bot.offset_ms = float(offset)
            self._bot.keybinds  = {int(k): v for k, v in kb.items()}

        self._start_hotkeys()
        self._log("Settings saved.")
        self._status.set("Settings saved.")
        messagebox.showinfo("Saved", "Settings saved!", parent=self)

    def _start_hotkeys(self):
        """Start global hotkey listener. Safe to call multiple times."""
        if not DEPS_OK:
            return
        if not self._global_hk.get():
            return

        hk  = self._settings.get("hotkeys", _DEFAULTS["hotkeys"])
        ss  = hk.get("start_stop",  "f1").upper()
        ou  = hk.get("offset_up",   "f2").upper()
        od  = hk.get("offset_down", "f3").upper()

        try:
            self._hk_hint.config(
                text="{}=Play  {}=+ms  {}=-ms".format(ss, ou, od))
        except Exception:
            pass

        # bot_engine.start_global_hotkeys() has its own internal guard
        start_global_hotkeys(
            on_f1=lambda: self.after(0, self._toggle_play),
            on_f2=lambda: self.after(0, self._inc_offset),
            on_f3=lambda: self.after(0, self._dec_offset),
            log=self._log,
        )

    # ── Chart load ────────────────────────────────────────────────────
    def _browse(self):
        path = filedialog.askopenfilename(
            title="Open FNF Chart",
            filetypes=[
                ("FNF Charts", "*.json *.fnfc *.zip"),
                ("JSON",       "*.json"),
                ("FNFC",       "*.fnfc"),
                ("ZIP",        "*.zip"),
                ("All files",  "*.*"),
            ]
        )
        if path:
            self._load_chart(path)

    def _ask_difficulty(self, options):
        """Modal popup to pick a difficulty. Returns the chosen name, or None if cancelled."""
        result = {"choice": None}

        dlg = tk.Toplevel(self)
        dlg.title("Select Difficulty")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self)
        dlg.grab_set()

        tk.Label(dlg, text="This chart has multiple difficulties.\nWhich one should the bot play?",
                 bg=BG, fg=TEXT, font=F_MAIN, justify="left").pack(padx=20, pady=(16, 10))

        def choose(name):
            result["choice"] = name
            dlg.destroy()

        for name in options:
            tk.Button(dlg, text=name.upper(), command=lambda n=name: choose(n),
                      bg=ACCENT, fg="white", font=F_BOLD,
                      relief="flat", cursor="hand2", pady=8
                      ).pack(padx=20, pady=4, fill="x")

        dlg.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        self.update_idletasks()
        x = self.winfo_x() + (self.winfo_width() // 2) - 120
        y = self.winfo_y() + (self.winfo_height() // 2) - 80
        dlg.geometry("+{}+{}".format(max(x, 0), max(y, 0)))

        self.wait_window(dlg)
        return result["choice"]

    def _load_chart(self, path):
        if not DEPS_OK:
            self._log("[ERROR] Core modules missing. Run install.bat.", "err")
            return

        # Multi-difficulty charts need the popup on the main thread, BEFORE
        # the background load thread starts (Tk dialogs only work here).
        try:
            options = ChartParser.list_difficulties(path)
        except Exception:
            options = []

        difficulty = None
        if options:
            if len(options) == 1:
                difficulty = options[0]
            else:
                difficulty = self._ask_difficulty(options)
                if difficulty is None:
                    self._log("Chart load cancelled.", "warn")
                    self._status.set("Ready \u2014 Load a chart to begin")
                    return

        self._status.set("Loading chart... Please wait...")
        self._log("Loading chart...", "info")

        def do_load():
            try:
                song         = ChartParser.load(path, difficulty=difficulty)
                skip_harmful = self._skip_harmful_v.get()
                notes        = song.playable_notes(skip_harmful=skip_harmful)
                all_notes    = song.all_player_notes()
                found_types  = song.all_note_types()

                skipped_harmful  = sum(1 for n in all_notes if n.is_harmful)
                skipped_opponent = sum(1 for n in all_notes if n.type_class == "opponent")

                def update_ui(n=notes, ft=found_types, sh=skipped_harmful, so=skipped_opponent):
                    self._song = song
                    self._chart_lbl.config(text=os.path.basename(path))
                    self._info_lbl.config(text=(
                        "Song:  {}\n"
                        "BPM:   {:.1f}\n"
                        "Notes: {}\n"
                        "Sects: {}"
                    ).format(song.song_name, song.bpm, len(n), len(song.sections)))
                    self._canvas.set_notes(n)
                    self._notes_var.set("Notes: {}".format(len(n)))

                    # Build note types summary for settings tab label
                    if ft:
                        custom_now = self._settings.get("custom_notes", {})
                        lines_t = ["Found note types:"]
                        for nt in ft:
                            from fnf_song import classify_note_type
                            cls = classify_note_type(nt)
                            override = custom_now.get(nt, None)
                            if cls == "opponent":
                                action = "SKIPPED (opponent)"
                            elif override is not None:
                                action = "clicked (custom override ON)" if override else "SKIPPED (custom override OFF)"
                            elif cls == "harmful":
                                action = "SKIPPED (harmful)" if skip_harmful else "clicked (skip OFF)"
                            elif cls == "cosmetic":
                                action = "clicked (cosmetic)"
                            else:
                                action = "clicked"
                            lines_t.append("  {}  ->  {}".format(nt, action))
                        if so:
                            lines_t.append("Opponent-tagged notes skipped: {}".format(so))
                        if sh and skip_harmful:
                            lines_t.append("Harmful notes skipped: {}".format(sh))
                        type_text = "\n".join(lines_t)
                    else:
                        type_text = "No custom note types — all notes are plain."

                    try:
                        self._note_types_lbl.config(text=type_text)
                    except Exception:
                        pass

                    self._rebuild_custom_note_checks(ft)

                    self._log("Loaded: {}  ({} notes, {} skipped)".format(
                        song.song_name, len(n), so + (sh if skip_harmful else 0)))
                    self._status.set("Ready! Press F1 to start")

                self.after(0, update_ui)

            except Exception as exc:
                msg = str(exc).strip() or repr(exc)
                def show_error(m=msg):
                    self._log("[ERROR] {}".format(m), "err")
                    self._status.set("Load failed.")
                    messagebox.showerror("Load Error",
                                         "Failed to load chart:\n\n{}".format(m),
                                         parent=self)
                self.after(0, show_error)

        threading.Thread(target=do_load, daemon=True).start()

    # ── Playback ──────────────────────────────────────────────────────
    def _toggle_play(self):
        if self._bot and self._bot.playing:
            self._bot.stop()
            self._play_btn.config(text="START  (F1)", bg="#136128")
            self._status.set("Stopped.")
        else:
            self._start_bot()
            
    def _classify_keep(self, n, skip_harmful, custom):
        """Returns (keep: bool, reason: str) for a note under current filters."""
        if n.type_class == "opponent":
            return False, "opponent"
        override = custom.get(n.note_type or "", None)
        if override is not None:
            return override, "custom"
        if n.type_class == "harmful":
            return (not skip_harmful), "harmful"
        return True, "normal"

    def _start_bot(self):
        if not self._song:
            self._log("No chart loaded. Use Browse Chart first.", "warn")
            return
        if not DEPS_OK:
            self._log("[ERROR] Missing modules. Run install.bat.", "err")
            return

        skip_harmful = self._skip_harmful_v.get()
        custom       = self._settings.get("custom_notes", {})
        all_notes    = self._song.all_player_notes()

        notes = []
        skip_counts = {"opponent": 0, "harmful": 0, "custom": 0}
        for n in all_notes:
            keep, reason = self._classify_keep(n, skip_harmful, custom)
            if keep:
                notes.append(n)
            elif reason in skip_counts:
                skip_counts[reason] += 1

        if skip_counts["custom"]:
            self._log("Custom overrides skipped {} note(s).".format(skip_counts["custom"]), "warn")
            
        if not notes:
            self._log("No playable notes found in chart! Cannot start bot.", "err")
            self._status.set("Error: Chart has no playable notes!")
            return

        # Stop any previous bot gracefully first
        if self._bot and self._bot.playing:
            self._bot.stop()
            time.sleep(0.05)

        # Keybinds from settings entries
        fallback = ["left", "down", "up", "right"]
        kb = {}
        for lane, v in self._key_vars.items():
            raw = (v.get() or "").strip()
            kb[lane] = raw if raw else fallback[lane]

        offset = self._get_offset()

        # Get fast_mode from settings
        fast_mode = bool(self._settings.get("fast_mode", False))

        self._bot             = BotEngine(self._song, fast_mode=fast_mode)
        self._bot.keybinds    = kb
        self._bot.offset_ms   = offset
        self._bot.on_log      = self._log
        self._bot.on_note_hit = self._on_hit
        self._bot.on_finished = self._on_finished
        self._bot.on_tick     = self._on_tick
        self._bot.start(notes=notes)   # pass pre-filtered list directly

        self._play_btn.config(text="STOP  (F1)", bg="#7a1515")
        self._status.set("Playing: {}".format(self._song.song_name))

    def _get_offset(self):
        try:
            val = self._offset_var.get().strip()
            return float(val) if val else 25.0
        except (ValueError, TypeError):
            return 25.0

    def _inc_offset(self):
        v = self._get_offset() + 5
        self._offset_var.set(str(int(v)))
        if self._bot:
            self._bot.offset_ms = v
        self._log("Offset: {:.0f} ms".format(v))

    def _dec_offset(self):
        v = self._get_offset() - 5
        self._offset_var.set(str(int(v)))
        if self._bot:
            self._bot.offset_ms = v
        self._log("Offset: {:.0f} ms".format(v))

    # ── Bot callbacks (called from worker thread — always use after()) ─
    def _on_hit(self, note):
        lane = note.lane % 4
        names = ["LEFT", "DOWN", "UP", "RIGHT"]
        # Capture lane & note.time by value in the default arg to avoid closure bug
        self._log("  -> {} @ {:.0f} ms".format(names[lane], note.time), "hit")
        self.after(0, lambda l=lane: self._canvas.flash_lane(l))

    def _on_finished(self):
        self.after(0, self._on_finished_ui)

    def _on_finished_ui(self):
        self._play_btn.config(text="START  (F1)", bg="#136128")
        self._status.set("Song complete!")

    def _on_tick(self, ms):
        # Throttle UI updates to ~30 fps from the potentially fast tick
        now = time.perf_counter()
        if now - self._last_tick_ui < 0.033:
            return
        self._last_tick_ui = now

        # Capture ms by value
        self.after(0, lambda t=ms: self._update_tick_ui(t))

    def _update_tick_ui(self, ms):
        total_s = int(ms / 1000)
        millis  = int(ms % 1000)
        mins    = total_s // 60
        secs    = total_s % 60
        self._time_var.set("{:02d}:{:02d}.{:03d}".format(mins, secs, millis))
        self._canvas.update_time(ms)

    # ── Logger ────────────────────────────────────────────────────────
    def _log(self, msg, tag="info"):
        """Thread-safe logger. Can be called from any thread."""
        def _write(m=msg, t=tag):
            try:
                ts = time.strftime("%H:%M:%S")
                self._console.config(state="normal")
                self._console.insert("end", "[{}] {}\n".format(ts, m), t)
                # Trim console to last 500 lines to prevent memory growth
                lines = int(self._console.index("end-1c").split(".")[0])
                if lines > 500:
                    self._console.delete("1.0", "{}.0".format(lines - 500))
                self._console.see("end")
                self._console.config(state="disabled")
            except Exception:
                pass
        self.after(0, _write)


# ── Entry point ───────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        app = FNFBotApp()
        app.mainloop()
    except Exception as exc:
        try:
            _r = tk.Tk()
            _r.withdraw()
            messagebox.showerror(
                "Fatal Error",
                "FNFBot crashed:\n\n{}\n\n{}".format(exc, traceback.format_exc()),
            )
            _r.destroy()
        except Exception:
            pass
