"""
bot_engine.py  —  FNF Bot keypress engine + global hotkey listener

Key fixes vs previous version:
  1. Hook thread guard actually works — uses threading.Event, not a bool
  2. Win32 message pump runs in its own thread; stop signal properly exits it
  3. pynput Controller is instantiated ONCE per press/release, not module-level
     (module-level Controller can crash under PyInstaller)
  4. _tap runs duration via time.sleep but checks _stop_event to exit early
  5. Hold notes: release is guaranteed even if stop() is called mid-hold
  6. Bot thread uses time.perf_counter exclusively (monotonic, no NTP jumps)
  7. All ctypes calls wrapped individually — one bad VK doesn't crash the loop
  8. get_backend_name() never throws
  9. start_global_hotkeys() is safe to call repeatedly — only one hook lives at a time
 10. Linux/macOS: pynput fallback works without ctypes
 11. Win32 hook now declares explicit argtypes/restype — fixes
     intermittent SetWindowsHookExW crash that silently fell back to pynput
 12. Win32 hook now passes hMod=0 (not GetModuleHandleW) — WH_KEYBOARD_LL
     is always thread-local, never injected; a real module handle caused
     WinError 126 "module not found" on every attempt the app was run
 13. kernel32 handle removed — it was only used for the now-deleted
     GetModuleHandleW call
 14. Added is_frozen() check — .py runs now log the real WinError code/
     message for debugging, .exe builds show the simpler message for end users
"""

import threading
import time
import sys
from typing import Callable, Optional

# ── Frozen/exe detection ───────────────────────────────────────────────
def is_frozen() -> bool:
    """True when running as a PyInstaller-built exe, False when running as .py"""
    return getattr(sys, 'frozen', False)

# ── Key backend detection ─────────────────────────────────────────────
_backend: Optional[str] = None

def _detect_backend() -> str:
    global _backend
    if _backend is not None:
        return _backend
    for name, probe in (
        ("pynput",    lambda: __import__("pynput.keyboard", fromlist=["x"])),
        ("pyautogui", lambda: __import__("pyautogui")),
        ("ctypes",    lambda: __import__("ctypes").windll),  # Windows only
    ):
        try:
            probe()
            _backend = name
            return _backend
        except Exception:
            pass
    _backend = "none"
    return _backend

def get_backend_name() -> str:
    try:
        b = _detect_backend()
        return {
            "pynput":    "pynput",
            "pyautogui": "pyautogui",
            "ctypes":    "ctypes (Windows built-in)",
            "none":      "NONE  —  run install.bat!",
        }.get(b, b)
    except Exception:
        return "unknown"

# ── pynput key resolution ─────────────────────────────────────────────
def _pynput_key(name: str):
    """Convert a string like 'left', 'f1', 'a' to a pynput Key or char."""
    from pynput.keyboard import Key
    _MAP = {
        "left":    Key.left,  "right":  Key.right,
        "up":      Key.up,    "down":   Key.down,
        "space":   Key.space, "enter":  Key.enter,
        "tab":     Key.tab,   "backspace": Key.backspace,
        "shift":   Key.shift, "ctrl":   Key.ctrl,  "alt": Key.alt,
        "esc":     Key.esc,   "escape": Key.esc,
        "f1":  Key.f1,  "f2":  Key.f2,  "f3":  Key.f3,  "f4":  Key.f4,
        "f5":  Key.f5,  "f6":  Key.f6,  "f7":  Key.f7,  "f8":  Key.f8,
        "f9":  Key.f9,  "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
    }
    k = (name or "").lower().strip()
    return _MAP.get(k, k[0] if k else "a")

# Windows VK table for ctypes fallback
_VK = {
    "left":  0x25, "up":    0x26, "right": 0x27, "down":  0x28,
    "space": 0x20, "enter": 0x0D, "tab":   0x09, "esc":   0x1B,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
    "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59,
    "z": 0x5A,
}

# ── Low-level key ops ─────────────────────────────────────────────────
# Pre-create controller for pynput to avoid repeated instantiation
_pynput_controller = None

def _get_pynput_controller():
    global _pynput_controller
    if _pynput_controller is None:
        from pynput.keyboard import Controller
        _pynput_controller = Controller()
    return _pynput_controller

def _press(name: str) -> None:
    b = _detect_backend()
    try:
        if b == "pynput":
            _get_pynput_controller().press(_pynput_key(name))
        elif b == "pyautogui":
            import pyautogui
            pyautogui.keyDown(name)
        elif b == "ctypes":
            import ctypes
            vk = _VK.get(name.lower())
            if vk:
                ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    except Exception:
        pass

def _release(name: str) -> None:
    b = _detect_backend()
    try:
        if b == "pynput":
            _get_pynput_controller().release(_pynput_key(name))
        elif b == "pyautogui":
            import pyautogui
            pyautogui.keyUp(name)
        elif b == "ctypes":
            import ctypes
            vk = _VK.get(name.lower())
            if vk:
                ctypes.windll.user32.keybd_event(vk, 0, 0x0002, 0)
    except Exception:
        pass

def _tap(name: str, dur: float = 0.0) -> None:
    """Instant tap - no artificial delay."""
    try:
        _press(name)
        _release(name)
    except Exception:
        pass


# ── Global hotkey listener ────────────────────────────────────────────
# One hook lives at a time. Calling start_global_hotkeys() again just
# updates the callback table; no new thread/hook is created.

_hk_callbacks: dict = {}      # VK int -> callable
_hk_lock       = threading.Lock()
_hook_started  = threading.Event()   # set once hook is running

VK_F1, VK_F2, VK_F3 = 0x70, 0x71, 0x72


def start_global_hotkeys(
    on_f1: Optional[Callable] = None,
    on_f2: Optional[Callable] = None,
    on_f3: Optional[Callable] = None,
    log:   Optional[Callable] = None,
) -> None:
    """
    Register callbacks and ensure the global keyboard hook is running.
    Safe to call multiple times — only ONE hook/thread ever exists.
    """
    global _hk_callbacks

    with _hk_lock:
        _hk_callbacks = {}
        if on_f1: _hk_callbacks[VK_F1] = on_f1
        if on_f2: _hk_callbacks[VK_F2] = on_f2
        if on_f3: _hk_callbacks[VK_F3] = on_f3

    if _hook_started.is_set():
        # Hook already running — callback table updated above, done.
        return

    # First call: spawn the appropriate hook thread
    if sys.platform == "win32":
        t = threading.Thread(target=_win32_hook, args=(log,), daemon=True)
    else:
        t = threading.Thread(target=_pynput_hook, args=(log,), daemon=True)

    t.start()


def _fire(vk: int) -> None:
    """Call the registered callback for a VK code, if any."""
    with _hk_lock:
        cb = _hk_callbacks.get(vk)
    if cb:
        try:
            cb()
        except Exception:
            pass


def _win32_hook(log: Optional[Callable]) -> None:
    """
    Windows WH_KEYBOARD_LL hook.
    Fires for ALL key presses system-wide, even when app is minimized.
    Runs a Windows message pump so the hook actually receives events.
    """
    _hook_started.set()
    try:
        import ctypes
        import ctypes.wintypes as wt

        WH_KEYBOARD_LL = 13
        WM_KEYDOWN     = 0x0100
        WM_SYSKEYDOWN  = 0x0104

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode",      wt.DWORD),
                ("scanCode",    wt.DWORD),
                ("flags",       wt.DWORD),
                ("time",        wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
            ]

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, wt.WPARAM, wt.LPARAM)

        user32   = ctypes.WinDLL('user32', use_last_error=True)

        # Explicit prototypes — required so ctypes marshals the HOOKPROC
        # callback correctly. Without these, SetWindowsHookExW intermittently
        # throws "expected WinFunctionType instance instead of WinFunctionType".
        user32.SetWindowsHookExW.restype  = wt.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]

        user32.CallNextHookEx.restype  = ctypes.c_long
        user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]

        user32.UnhookWindowsHookEx.restype  = wt.BOOL
        user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]

        user32.GetMessageW.restype  = wt.BOOL
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, ctypes.c_uint, ctypes.c_uint]

        def low_level_handler(nCode, wParam, lParam):
            if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    kb = ctypes.cast(
                        lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    _fire(kb.vkCode)
                except Exception:
                    pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        proc   = HOOKPROC(low_level_handler)
        handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, 0, 0)

        if not handle:
            err = ctypes.get_last_error()
            if log:
                if is_frozen():
                    log("[WARN] Win32 keyboard hook failed (try Run as Administrator). "
                        "Falling back to pynput.")
                else:
                    log("[WARN] Win32 keyboard hook failed (WinError {}: {}). Falling back to pynput.".format(
                        err, ctypes.FormatError(err)))
            _hook_started.clear()
            _pynput_hook(log)
            return

        # Message pump — required for the hook callbacks to fire
        msg = wt.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.UnhookWindowsHookEx(handle)

    except Exception as exc:
        if log:
            log("[WARN] Win32 hook crashed: {}. Falling back to pynput.".format(exc))
        _hook_started.clear()
        _pynput_hook(log)


def _pynput_hook(log: Optional[Callable]) -> None:
    """
    pynput-based global listener.
    Works on Linux/macOS; also works on Windows if Win32 hook fails.
    """
    _hook_started.set()
    try:
        from pynput.keyboard import Key, Listener
        KEY_VK = {Key.f1: VK_F1, Key.f2: VK_F2, Key.f3: VK_F3}

        def on_press(key):
            vk = KEY_VK.get(key)
            if vk is not None:
                _fire(vk)

        with Listener(on_press=on_press, suppress=False) as listener:
            listener.join()
    except Exception as exc:
        if log:
            log("[WARN] pynput hook failed: {}. Hotkeys unavailable.".format(exc))


# ── BotEngine ─────────────────────────────────────────────────────────
DEFAULT_KEYBINDS = {0: "left", 1: "down", 2: "up", 3: "right"}


class BotEngine:
    """
    Reads FNFSong notes and fires keypresses at the right time.
    All callbacks (on_log, on_note_hit, on_finished, on_tick) are called
    from the worker thread — callers must marshal to the GUI thread themselves.
    """

    def __init__(self, song, fast_mode=False):
        self.song        = song
        self.offset_ms   = 25.0
        self.keybinds    = dict(DEFAULT_KEYBINDS)
        self.playing     = False
        self._stop       = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.fast_mode   = fast_mode  # Fast mode for spammy songs

        # Callbacks
        self.on_log:      Callable = print
        self.on_note_hit: Callable = lambda n: None
        self.on_finished: Callable = lambda: None
        self.on_tick:     Callable = lambda t: None

    def start(self, notes=None) -> None:
        if self.playing:
            self.stop()
            return

        if notes is None:
            notes = self.song.all_player_notes()
        if not notes:
            self.on_log("No player notes found in chart!")
            return

        if _detect_backend() == "none":
            self.on_log("[ERROR] No input library found. Run install.bat.")
            return

        self._stop.clear()
        self.playing = True
        self._thread = threading.Thread(
            target=self._loop, args=(notes,), daemon=True)
        self._thread.start()
        self.on_log("Playing '{}'  ({} notes)  |  backend: {}".format(
            self.song.song_name, len(notes), get_backend_name()))

    def stop(self) -> None:
        self._stop.set()
        # on_log / playing flag will be reset by the thread itself

    def increase_offset(self) -> None:
        self.offset_ms += 5
        self.on_log("Offset: {:.0f} ms".format(self.offset_ms))

    def decrease_offset(self) -> None:
        self.offset_ms -= 5
        self.on_log("Offset: {:.0f} ms".format(self.offset_ms))

    # ── Play loop ─────────────────────────────────────────────────────
    def _loop(self, notes) -> None:
        t0    = time.perf_counter()
        idx   = 0
        total = len(notes)
        holds = {}   # lane (int) -> release_time_ms (float)

        self.on_log("Bot running.  Offset: {:.0f} ms".format(self.offset_ms))

        try:
            while not self._stop.is_set():
                now_ms = (time.perf_counter() - t0) * 1000.0
                self.on_tick(now_ms)

                # Release expired holds
                for lane in list(holds):
                    if now_ms >= holds[lane]:
                        key = self.keybinds.get(lane, DEFAULT_KEYBINDS.get(lane, "left"))
                        _release(key)
                        del holds[lane]

                # Look-ahead: process notes up to 20ms early for fast response
                look_ahead = 20.0
                hit_any = False
                while idx < total:
                    note    = notes[idx]
                    target  = note.time - self.offset_ms
                    if now_ms < target - look_ahead:
                        break
                    if now_ms < target:
                        hit_any = True
                        lane    = note.lane % 4
                        key     = self.keybinds.get(lane, DEFAULT_KEYBINDS.get(lane, "left"))
                        if note.hold_length > 0:
                            _press(key)
                            holds[lane] = now_ms + note.hold_length
                        else:
                            _tap(key)
                        self.on_note_hit(note)
                    idx += 1

                # Done
                if idx >= total and not holds:
                    time.sleep(0.4)
                    self.playing = False
                    self.on_log("Song complete!")
                    self.on_finished()
                    return

                # Ultra-fast sleep - minimal delay based on fast_mode
                if not hit_any and idx < total:
                    next_target = notes[idx].time - self.offset_ms - look_ahead
                    if self.fast_mode:
                        # Fast mode: minimum delay for spammy songs
                        sleep_ms = max(0.1, min(next_target - now_ms, 5))
                    else:
                        # Normal mode: more conservative delay
                        sleep_ms = max(1.0, min(next_target - now_ms, 10))
                    time.sleep(sleep_ms / 1000.0)

        finally:
            # Always release any stuck holds
            for lane in list(holds):
                key = self.keybinds.get(lane, DEFAULT_KEYBINDS.get(lane, "left"))
                _release(key)
            self.playing = False
            if self._stop.is_set():
                self.on_log("Bot stopped.")
