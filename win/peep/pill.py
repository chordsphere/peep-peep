"""The REC pill and the agent's toasts: small always-on-top, click-through
windows, excluded from screen capture.

The pill (`● 00:12` on red) is visible to the user and absent from the
recording: `winapi.exclude_from_capture` sets WDA_EXCLUDEFROMCAPTURE. The
session-B probe (2026-10-05) drew squares with and without the affinity,
including this exact layered + click-through recipe, and grabbed frames
through ddagrab: the control square was captured, both excluded squares
were not. That is why `agent.pill` defaults to true.

If the affinity cannot be set on a given machine (the read-back is not
WDA_EXCLUDEFROMCAPTURE), the overlay refuses to show itself rather than
pollute the recording, and the agent reports it. "Optional" is the
`[agent] pill` config flag, never a code change.

Toasts reuse the same window recipe for short messages: a hotkey that
cannot be registered, "mark 2", "discarded", a failed recording. They
are excluded from capture too, since they can appear mid-recording.

Pure helpers (tested on WSL): `format_elapsed`, `overlay_position`.

Sections:
  1. Pure helpers               (~line 40)
  2. Tk overlays                (~line 85)
"""

from __future__ import annotations

import logging

from .logsetup import event

log = logging.getLogger("peep.pill")

# ---------------------------------------------------------------------------
# 1. Pure helpers
# ---------------------------------------------------------------------------

COLORS = {"recording": "#C62828", "starting": "#6D4C41", "stopping": "#455A64",
          "info": "#263238", "warning": "#8D6E00", "error": "#B71C1C",
          # Session C1a: a hard pause must be unmistakable at a glance, so it is not red.
          "paused": "#E65100", "pausing": "#455A64", "resuming": "#6D4C41"}


def format_elapsed(seconds: float | None) -> str:
    """12.7 -> '00:12'; 3725 -> '1:02:05'; None/negative -> '00:00'."""
    s = 0 if seconds is None or seconds < 0 else int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def take_text(takes: int, take_open: bool) -> str:
    """'' with no take yet; '◉ take 3' while take 3 is open; '○ 3 takes' between takes."""
    if take_open:
        return f"◉ take {takes}"
    if takes:
        return f"○ {takes} take{'s' if takes != 1 else ''}"
    return ""


def pill_text(state: str, elapsed_s: float | None, takes: int = 0, take_open: bool = False) -> str:
    """The pill's text. `elapsed_s` is the time captured so far (the segments before
    a pause plus the current one), so it stands still while paused."""
    t = take_text(takes, take_open)
    tail = f"  {t}" if t else ""
    if state == "recording":
        return f"● {format_elapsed(elapsed_s)}{tail}"
    if state == "paused":
        return f"❚❚ PAUSED  {format_elapsed(elapsed_s)}{tail}"
    if state == "pausing":
        return "❚❚ pausing…"
    if state == "resuming":
        return "● resuming…"
    if state == "starting":
        return "● starting…"
    if state == "stopping":
        return "■ saving…"
    return state


def overlay_position(position: str, screen_w: int, screen_h: int, w: int, h: int, margin: int,
                     stack: int = 0, gap: int = 8) -> tuple[int, int]:
    """Top-left pixel for a w x h overlay at `position` (config.PILL_POSITIONS).
    `stack` is how many overlay heights (plus gap) to move away from the edge,
    so a toast sits just inside the pill rather than on top of it."""
    vertical, _, horizontal = position.partition("-")
    if horizontal == "left":
        x = margin
    elif horizontal == "center":
        x = (screen_w - w) // 2
    else:
        x = screen_w - margin - w
    offset = stack * (h + gap)
    y = margin + offset if vertical == "top" else screen_h - margin - h - offset
    return max(0, x), max(0, y)


# ---------------------------------------------------------------------------
# 2. Tk overlays
# ---------------------------------------------------------------------------


class Overlay:
    """One click-through, capture-excluded, topmost label window.

    `excluded` is None until the window is first shown; False means the
    affinity did not take, and the overlay will not show itself again."""

    def __init__(self, master, font=("Segoe UI", 11, "bold"), alpha: float = 0.92, padx: int = 14, pady: int = 6,
                 require_exclusion: bool = True):
        import tkinter as tk
        self.require_exclusion = require_exclusion
        self.tk = tk
        self.top = tk.Toplevel(master)
        self.top.withdraw()
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", alpha)
        self.label = tk.Label(self.top, text="", fg="white", bg=COLORS["info"], font=font, padx=padx, pady=pady)
        self.label.pack()
        self.excluded: bool | None = None
        self.visible = False

    def _secure(self) -> bool:
        """Apply click-through + capture exclusion once, on first show."""
        if self.excluded is not None:
            return self.excluded
        from . import winapi
        # Tk creates the top-level wrapper HWND only when the window is first
        # mapped. Map it fully transparent, secure it, then withdraw: nothing
        # un-excluded is ever drawn, so nothing can land in a recording.
        alpha = self.top.attributes("-alpha")
        self.top.attributes("-alpha", 0.0)
        self.top.deiconify()
        self.top.update()
        hwnd = winapi.toplevel_hwnd(self.top.winfo_id())
        winapi.make_click_through(hwnd)
        self.excluded = winapi.exclude_from_capture(hwnd)
        self.top.withdraw()
        self.top.attributes("-alpha", alpha)
        if not self.excluded:
            event(log, logging.ERROR, "overlay.not_excluded", hwnd=hwnd)
        return self.excluded

    def prepare(self) -> bool:
        """Secure the window up front (the agent does this at start, before any
        recording), so the first real show is instant. Returns `excluded`."""
        return self._secure()

    def show(self, text: str, color: str, position: str, margin: int, stack: int = 0) -> bool:
        """Show or update; False (and nothing shown) if capture exclusion failed
        and this overlay requires it (the pill and the in-recording toast do)."""
        if not self._secure() and self.require_exclusion:
            return False
        if self.label.cget("text") != text or self.label.cget("bg") != color:
            self.label.configure(text=text, bg=color)
        self.top.update_idletasks()
        w, h = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
        x, y = overlay_position(position, self.top.winfo_screenwidth(), self.top.winfo_screenheight(),
                                w, h, margin, stack)
        self.top.geometry(f"+{x}+{y}")
        if not self.visible:
            self.top.deiconify()
            self.top.attributes("-topmost", True)
            self.visible = True
        return True

    def hide(self) -> None:
        if self.visible:
            self.top.withdraw()
            self.visible = False

    def destroy(self) -> None:
        try:
            self.top.destroy()
        except self.tk.TclError as exc:
            event(log, logging.WARNING, "overlay.destroy_failed", error=str(exc))
