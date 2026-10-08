"""Global hotkeys for the resident agent: chord parsing, the action table,
and a RegisterHotKey listener on its own Win32 message-loop thread.

  parse_chord("Ctrl+Alt+R")       -> Chord(mods=MOD_CONTROL|MOD_ALT, vk=0x52, text="Ctrl+Alt+R")
  table_from_agent_config(cfg)    -> {"record": Chord, "mark": Chord, "discard": Chord,
                                     "pause": Chord, "take": Chord, "correct": Chord} (C1a;
                                     C1c renamed retake to correct; C1d adds learn_start and
                                     learn_end, and refuses Ctrl+Alt+Space, reserved for C1e),
                                     refusing two actions on one chord
  HotkeyListener                  registers the table on a dedicated thread and
                                  calls on_hotkey(action, foreground) for each press

Why a dedicated thread rather than Tk's: RegisterHotKey(NULL, ...) posts
WM_HOTKEY to the *registering thread's* queue, and Tk's main loop does not
hand thread messages to Python. A thread that owns a plain
MsgWaitForMultipleObjects/PeekMessage loop keeps the hotkeys independent
of Tk; the agent marshals each press onto the Tk thread itself.

The foreground window is read in the listener thread the instant WM_HOTKEY
arrives (A's Proposal 2): any later and the foreground may already be the
pill, the dialog or the agent.

A chord another app owns fails with ERROR_HOTKEY_ALREADY_REGISTERED
(1409). That is reported through `on_problem` (the agent logs it and shows
it on screen, naming the chord) and retried for `retry_s` seconds, because
at login the shell may not have settled yet; after that it is reported as
final. Nothing about a failed chord is silent.

Sections:
  1. Chords + table             (~line 40)
  2. Win32 message-loop API     (~line 135)
  3. Listener                   (~line 205)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .logsetup import event

log = logging.getLogger("peep.hotkeys")

# ---------------------------------------------------------------------------
# 1. Chords + table
# ---------------------------------------------------------------------------

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
ERROR_HOTKEY_ALREADY_REGISTERED = 1409
ACTIONS = ("record", "mark", "discard", "pause", "take", "correct", "learn_start", "learn_end")
# Chords no action may take: (mods, vk) -> why. C1e's expanded-pill toggle (ratified 2026-10-07).
RESERVED = {(0x2 | 0x1, 0x20): "Ctrl+Alt+Space is reserved for the expanded pill (session C1e)"}

_MODIFIERS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
              "win": MOD_WIN, "windows": MOD_WIN, "super": MOD_WIN}
_MOD_ORDER = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))

# Key name (lowercase) -> (virtual-key code, canonical spelling).
_NAMED_KEYS = {
    "space": (0x20, "Space"), "enter": (0x0D, "Enter"), "return": (0x0D, "Enter"), "tab": (0x09, "Tab"),
    "backspace": (0x08, "Backspace"), "insert": (0x2D, "Insert"), "ins": (0x2D, "Insert"),
    "delete": (0x2E, "Delete"), "del": (0x2E, "Delete"), "home": (0x24, "Home"), "end": (0x23, "End"),
    "pageup": (0x21, "PageUp"), "pgup": (0x21, "PageUp"), "pagedown": (0x22, "PageDown"),
    "pgdn": (0x22, "PageDown"), "up": (0x26, "Up"), "down": (0x28, "Down"), "left": (0x25, "Left"),
    "right": (0x27, "Right"), "pause": (0x13, "Pause"), "printscreen": (0x2C, "PrintScreen"),
    "prtsc": (0x2C, "PrintScreen"), "escape": (0x1B, "Escape"), "esc": (0x1B, "Escape"),
    "comma": (0xBC, "Comma"), ",": (0xBC, "Comma"), "period": (0xBE, "Period"), ".": (0xBE, "Period"),
    "minus": (0xBD, "Minus"), "-": (0xBD, "Minus"), "equals": (0xBB, "Equals"), "=": (0xBB, "Equals"),
    "semicolon": (0xBA, "Semicolon"), ";": (0xBA, "Semicolon"), "slash": (0xBF, "Slash"),
    "/": (0xBF, "Slash"), "backquote": (0xC0, "Backquote"), "`": (0xC0, "Backquote"),
    "quote": (0xDE, "Quote"), "'": (0xDE, "Quote"), "backslash": (0xDC, "Backslash"),
    "\\": (0xDC, "Backslash"), "[": (0xDB, "LeftBracket"), "]": (0xDD, "RightBracket"),
    # Session C1a: the numeric keypad (NumLock on; with it off, Windows sends Home/End/... instead)
    **{f"num{n}": (0x60 + n, f"Num{n}") for n in range(10)},
    **{f"numpad{n}": (0x60 + n, f"Num{n}") for n in range(10)},
    "nummultiply": (0x6A, "NumMultiply"), "numadd": (0x6B, "NumAdd"), "numplus": (0x6B, "NumAdd"),
    "numsubtract": (0x6D, "NumSubtract"), "numminus": (0x6D, "NumSubtract"),
    "numdecimal": (0x6E, "NumDecimal"), "numdivide": (0x6F, "NumDivide"),
}
# F13-F24 exist on no ordinary keyboard, so nothing types them: macro pads and mouse
# software can emit them, and they may be bound without a modifier.
BARE_OK_VK = range(0x7C, 0x88)


class HotkeyError(ValueError):
    """A chord that cannot be used, or two actions sharing one; says why."""


@dataclass(frozen=True)
class Chord:
    mods: int          # MOD_* bits, without MOD_NOREPEAT (added at registration)
    vk: int            # Windows virtual-key code
    text: str          # canonical spelling, e.g. "Ctrl+Alt+R"


def _key(name: str) -> tuple[int, str]:
    low = name.lower()
    if len(name) == 1 and name.isascii() and name.isalnum():
        return ord(name.upper()), name.upper()
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        n = int(low[1:])
        return 0x6F + n, f"F{n}"
    if low in _NAMED_KEYS:
        return _NAMED_KEYS[low]
    raise HotkeyError(f"unknown key {name!r} (use a letter, a digit, F1-F24, Num0-Num9, NumAdd, ..., or one of: "
                      f"Space, Enter, Tab, Backspace, Insert, Delete, Home, End, PageUp, PageDown, arrows, Pause, ...)")


def parse_chord(text: str) -> Chord:
    """'ctrl + alt + r' -> Chord(MOD_CONTROL|MOD_ALT, 0x52, 'Ctrl+Alt+R').

    Exactly one non-modifier key; at least one of Ctrl, Alt or Win, because a
    global hotkey on a bare key (or Shift+key) would swallow ordinary typing
    in every application. The exception is F13-F24, which nothing types."""
    if not isinstance(text, str) or not text.strip():
        raise HotkeyError("empty hotkey")
    # A trailing '+' key ("Ctrl+Alt++") is spelled "Equals"/"Plus" instead; split simply.
    parts = [p.strip() for p in text.split("+")]
    if any(not p for p in parts):
        raise HotkeyError(f"malformed hotkey {text!r} (expected e.g. 'Ctrl+Alt+R')")
    mods, keys = 0, []
    for p in parts:
        bit = _MODIFIERS.get(p.lower())
        if bit is not None:
            if mods & bit:
                raise HotkeyError(f"modifier {p!r} repeated in {text!r}")
            mods |= bit
        else:
            keys.append(p)
    if len(keys) != 1:
        raise HotkeyError(f"hotkey {text!r} needs exactly one non-modifier key, got {len(keys)}")
    vk, key_text = _key(keys[0])
    if not mods & (MOD_CONTROL | MOD_ALT | MOD_WIN) and vk not in BARE_OK_VK:
        raise HotkeyError(f"hotkey {text!r} needs Ctrl, Alt or Win; a bare key would steal typing everywhere "
                          f"(only F13-F24 may be bound alone)")
    canonical = "+".join([name for bit, name in _MOD_ORDER if mods & bit] + [key_text])
    return Chord(mods, vk, canonical)


def build_table(chords: dict[str, str]) -> dict[str, Chord]:
    """{action: chord text} -> {action: Chord}; refuses unknown actions and
    two actions bound to the same chord (the second registration would fail
    against our own first one, which is a confusing way to find out)."""
    table: dict[str, Chord] = {}
    seen: dict[tuple[int, int], str] = {}
    for action, text in chords.items():
        if action not in ACTIONS:
            raise HotkeyError(f"unknown hotkey action {action!r} (known: {', '.join(ACTIONS)})")
        try:
            chord = parse_chord(text)
        except HotkeyError as exc:
            raise HotkeyError(f"{action}_hotkey: {exc}") from None
        key = (chord.mods, chord.vk)
        if key in RESERVED:
            raise HotkeyError(f"{action}_hotkey: {RESERVED[key]}")
        if key in seen:
            raise HotkeyError(f"{action}_hotkey and {seen[key]}_hotkey are both {chord.text}")
        seen[key] = action
        table[action] = chord
    return table


def table_from_agent_config(agent_cfg) -> dict[str, Chord]:
    return build_table({"record": agent_cfg.record_hotkey, "mark": agent_cfg.mark_hotkey,
                        "discard": agent_cfg.discard_hotkey, "pause": agent_cfg.pause_hotkey,
                        "take": agent_cfg.take_hotkey, "correct": agent_cfg.correct_hotkey,
                        "learn_start": agent_cfg.learn_start_hotkey, "learn_end": agent_cfg.learn_end_hotkey})


def describe_error(chord: Chord, winerror: int) -> str:
    if winerror == ERROR_HOTKEY_ALREADY_REGISTERED:
        return f"{chord.text} is already taken by another app (winerror 1409)"
    return f"{chord.text} could not be registered (winerror {winerror})"


# ---------------------------------------------------------------------------
# 2. Win32 message-loop API
# ---------------------------------------------------------------------------


class Win32HotkeyApi:
    """The five Win32 calls the listener needs, all made from the listener
    thread except `post_quit`. Constructed lazily so importing this module
    never touches ctypes.windll (tests run on Linux)."""

    WM_HOTKEY, WM_QUIT, PM_REMOVE, PM_NOREMOVE, QS_ALLINPUT = 0x0312, 0x0012, 0x1, 0x0, 0x04FF

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ct, self._wt = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.user32.MsgWaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.BOOL,
                                                          wintypes.DWORD, wintypes.DWORD]
        self._msg = wintypes.MSG()

    def thread_id(self) -> int:
        # The first PeekMessage creates this thread's message queue, so a
        # post_quit from another thread can never be lost.
        self.user32.PeekMessageW(self._ct.byref(self._msg), None, 0, 0, self.PM_NOREMOVE)
        return self.k32.GetCurrentThreadId()

    def register(self, ident: int, mods: int, vk: int) -> int:
        """0 on success, else the Win32 error code."""
        if self.user32.RegisterHotKey(None, ident, mods | MOD_NOREPEAT, vk):
            return 0
        return self._ct.get_last_error() or -1

    def unregister(self, ident: int) -> None:
        self.user32.UnregisterHotKey(None, ident)

    def wait(self, timeout_ms: int) -> list[tuple]:
        """Messages that arrived within `timeout_ms`: ('hotkey', id) or ('quit',)."""
        self.user32.MsgWaitForMultipleObjects(0, None, False, timeout_ms, self.QS_ALLINPUT)
        out = []
        while self.user32.PeekMessageW(self._ct.byref(self._msg), None, 0, 0, self.PM_REMOVE):
            if self._msg.message == self.WM_HOTKEY:
                out.append(("hotkey", int(self._msg.wParam)))
            elif self._msg.message == self.WM_QUIT:
                out.append(("quit",))
        return out

    def post_quit(self, thread_id: int) -> bool:
        return bool(self.user32.PostThreadMessageW(thread_id, self.WM_QUIT, 0, 0))


# ---------------------------------------------------------------------------
# 3. Listener
# ---------------------------------------------------------------------------

ID_BASE = 0xB000       # RegisterHotKey ids for a NULL hwnd must be 0x0000-0xBFFF


class HotkeyListener:
    """Registers `table` on its own thread and reports presses.

    on_hotkey(action, foreground)   called on the listener thread; must be quick
                                    (the agent just queues it for the Tk thread)
    on_problem(action, chord, message, final)
                                    a chord that failed to register; `final` once
                                    the retry window has closed
    on_registered(action, chord)    a chord that registered (possibly after retries)
    """

    def __init__(self, table: dict[str, Chord], on_hotkey: Callable, on_problem: Callable,
                 on_registered: Callable | None = None, *, api_factory: Callable | None = None,
                 foreground: Callable | None = None, retry_s: float = 60, retry_every_s: float = 5,
                 clock: Callable[[], float] = time.monotonic):
        self.table = dict(table)
        self.on_hotkey, self.on_problem = on_hotkey, on_problem
        self.on_registered = on_registered or (lambda action, chord: None)
        self.api_factory = api_factory or Win32HotkeyApi
        if foreground is None:
            from . import winapi
            foreground = winapi.foreground_window
        self.foreground = foreground
        self.retry_s, self.retry_every_s, self.clock = retry_s, retry_every_s, clock
        self.ids = {ID_BASE + i: action for i, action in enumerate(sorted(self.table))}
        self.state: dict[str, str] = {a: "pending" for a in self.table}
        self._tid: int | None = None
        self._api = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    # -- control (any thread) --------------------------------------------------

    def start(self, wait_s: float = 5.0) -> None:
        self._thread = threading.Thread(target=self.run, name="hotkeys", daemon=True)
        self._thread.start()
        if not self._ready.wait(wait_s):
            event(log, logging.ERROR, "hotkeys.thread_not_ready", wait_s=wait_s)

    def stop(self, timeout_s: float = 5.0) -> None:
        if self._api is not None and self._tid is not None:
            if not self._api.post_quit(self._tid):
                event(log, logging.WARNING, "hotkeys.post_quit_failed", thread_id=self._tid)
        if self._thread is not None:
            self._thread.join(timeout_s)
            if self._thread.is_alive():
                event(log, logging.WARNING, "hotkeys.thread_still_running", timeout_s=timeout_s)

    def status(self) -> dict[str, str]:
        """{action: 'Ctrl+Alt+R registered' | '... retrying: ...' | '... FAILED: ...'} for agent.json."""
        return {a: f"{self.table[a].text} {s}" for a, s in self.state.items()}

    # -- the listener thread -----------------------------------------------------

    def _try_register(self, api, pending: set[str], final: bool) -> None:
        for ident, action in self.ids.items():
            if action not in pending:
                continue
            chord = self.table[action]
            err = api.register(ident, chord.mods, chord.vk)
            if err == 0:
                pending.discard(action)
                self.state[action] = "registered"
                event(log, logging.INFO, "hotkeys.registered", action=action, chord=chord.text)
                self.on_registered(action, chord)
                continue
            message = describe_error(chord, err)
            was = self.state[action]
            self.state[action] = ("FAILED: " if final else "retrying: ") + message
            if final or was == "pending":       # report the first failure and the final one, not every retry
                event(log, logging.ERROR, "hotkeys.register_failed", action=action, chord=chord.text,
                      winerror=err, final=final)
                self.on_problem(action, chord, message, final)

    def run(self) -> None:
        api = self.api_factory()
        self._api = api
        self._tid = api.thread_id()
        self._ready.set()
        pending = set(self.table)
        deadline = self.clock() + self.retry_s
        self._try_register(api, pending, final=self.retry_s <= 0)
        if self.retry_s <= 0:
            pending.clear()
        next_retry = self.clock() + self.retry_every_s
        try:
            while True:
                for msg in api.wait(500 if pending else 1000):
                    if msg[0] == "quit":
                        event(log, logging.INFO, "hotkeys.quit")
                        return
                    if msg[0] == "hotkey":
                        action = self.ids.get(msg[1])
                        if action is None:
                            event(log, logging.WARNING, "hotkeys.unknown_id", ident=msg[1])
                            continue
                        fg = self.foreground() if action == "record" else None
                        event(log, logging.INFO, "hotkeys.pressed", action=action, chord=self.table[action].text)
                        self.on_hotkey(action, fg)
                if pending:
                    now = self.clock()
                    if now >= deadline:
                        self._try_register(api, pending, final=True)
                        pending.clear()
                    elif now >= next_retry:
                        self._try_register(api, pending, final=False)
                        next_retry = now + self.retry_every_s
        finally:
            for ident, action in self.ids.items():
                if self.state.get(action) == "registered":
                    api.unregister(ident)
            event(log, logging.INFO, "hotkeys.unregistered")
