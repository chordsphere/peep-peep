"""The clapper flash: a borderless, full-screen, solid-colour window shown
for a few frames at record start and stop, so session C can find the
recording's true start/stop by colour alone.

The window is deliberately captured (no WDA_EXCLUDEFROMCAPTURE — that is
B's REC pill). The probe measured a 150 ms magenta flash landing in 4-5
frames at 30 fps with exact colour and full corner-to-corner coverage,
using exactly this recipe: per-monitor DPI awareness first, then an
overrideredirect Tk window at the physical screen size, topmost.

`Flasher` is the interface the recorder uses; `TkFlasher` is the real
one (Windows, needs a desktop), `NullFlasher` records timings without
showing anything (used for --no-flash and in tests). Both return the
agent-side timestamps the sidecar stores.

Session C1a adds the corner patch, `patch(color, duration_ms, corner,
size_px, margin_px)`: the same window, captured on purpose, shrunk to a
solid square in one corner for 200 ms. Takes, retakes and marks use it, so
narrating with frequent takes is not a full-screen strobe. Its geometry is
recorded in physical pixels (the process is DPI-aware) for C1b to sample.
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from dataclasses import dataclass

from .logsetup import event

log = logging.getLogger("peep.flash")


@dataclass(frozen=True)
class FlashRecord:
    color: str
    duration_ms: int              # requested
    shown_at: str                 # ISO local time when the window was on screen
    shown_mono: float             # time.monotonic() at the same instant
    hidden_mono: float            # time.monotonic() when it was withdrawn
    shown: bool                   # False for NullFlasher / failures
    # Session A.1: the same instant on the QPC clock (time.perf_counter on Windows), the
    # clock the audio children stamp samples with, so C can relate flash and audio
    # exactly. time.monotonic is GetTickCount64 on Python 3.12 (15.6 ms steps).
    shown_qpc: float | None = None
    # Session C1a: "full" (the whole screen) or "patch" (a corner square). For a patch,
    # rect = (x, y, w, h) and screen = (w, h), both in physical pixels.
    style: str = "full"
    rect: tuple | None = None
    screen: tuple | None = None

    def to_sidecar(self, t0_mono: float) -> dict:
        """Sidecar form; times relative to the ffmpeg launch (t0). A full flash keeps
        exactly A's keys; a patch adds style, rect and screen."""
        out = {"color": self.color, "duration_ms": self.duration_ms, "shown": self.shown,
               "shown_at": self.shown_at,
               "since_ffmpeg_start_s": round(self.shown_mono - t0_mono, 3),
               "actual_ms": round((self.hidden_mono - self.shown_mono) * 1000, 1),
               "shown_qpc": round(self.shown_qpc, 6) if self.shown_qpc is not None else None}
        if self.style != "full":
            out["style"] = self.style
            out["rect"] = dict(zip(("x", "y", "w", "h"), self.rect)) if self.rect else None
            out["screen"] = dict(zip(("w", "h"), self.screen)) if self.screen else None
        return out


PATCH_OPPOSITE = {"top-right": "bottom-left", "top-left": "bottom-right", "bottom-right": "top-left",
                  "bottom-left": "top-right", "top-center": "bottom-left", "bottom-center": "top-left"}


def patch_corner(configured: str, pill_position: str) -> str:
    """The corner the patch goes in: as configured, or for "auto" the one
    opposite the pill (so the two never sit on top of each other)."""
    if configured != "auto":
        return configured
    return PATCH_OPPOSITE.get(pill_position, "bottom-left")


def patch_rect(corner: str, size: int, screen_w: int, screen_h: int, margin: int = 0) -> tuple[int, int, int, int]:
    """(x, y, w, h) of a size x size square in `corner` of a screen_w x screen_h
    screen, `margin` pixels in from both edges; clamped to the screen."""
    size = max(1, min(size, screen_w, screen_h))
    vertical, _, horizontal = corner.partition("-")
    x = margin if horizontal == "left" else screen_w - margin - size
    y = margin if vertical == "top" else screen_h - margin - size
    return max(0, x), max(0, y), size, size


def _iso_now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


class NullFlasher:
    """Shows nothing; keeps the timeline shape identical to a real flash."""

    def prepare(self) -> None:
        pass

    def flash(self, color: str, duration_ms: int) -> FlashRecord:
        now = time.monotonic()
        return FlashRecord(color, duration_ms, _iso_now(), now, now, shown=False, shown_qpc=time.perf_counter())

    def patch(self, color: str, duration_ms: int, corner: str, size_px: int, margin_px: int = 0) -> FlashRecord:
        now = time.monotonic()
        return FlashRecord(color, duration_ms, _iso_now(), now, now, shown=False, shown_qpc=time.perf_counter(),
                           style="patch")

    def close(self) -> None:
        pass


class TkFlasher:
    """Real flash via tkinter. `prepare()` builds the (withdrawn) window up
    front so the first flash is not delayed by Tk start-up. Must be used
    from one thread (the recorder's main thread)."""

    def __init__(self):
        self.root = None
        self.size = None

    def prepare(self) -> None:
        from . import winapi
        winapi.set_dpi_aware()
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        w, h = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry(f"{w}x{h}+0+0")
        root.attributes("-topmost", True)
        try:
            root.configure(cursor="none")
        except tk.TclError as exc:
            event(log, logging.INFO, "flash.cursor_hide_unsupported", error=str(exc))
        root.update_idletasks()
        self.root, self.size = root, (w, h)
        event(log, logging.INFO, "flash.prepared", width=w, height=h)

    def flash(self, color: str, duration_ms: int) -> FlashRecord:
        if self.root is None:
            self.prepare()
        root = self.root
        root.configure(bg=color)
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        root.update()
        shown_qpc, shown_mono, shown_at = time.perf_counter(), time.monotonic(), _iso_now()
        deadline = shown_mono + duration_ms / 1000
        while time.monotonic() < deadline:
            root.update()
            time.sleep(0.004)
        root.withdraw()
        root.update()
        hidden = time.monotonic()
        event(log, logging.INFO, "flash.shown", color=color, requested_ms=duration_ms,
              actual_ms=round((hidden - shown_mono) * 1000, 1))
        return FlashRecord(color, duration_ms, shown_at, shown_mono, hidden, shown=True, shown_qpc=shown_qpc)

    def patch(self, color: str, duration_ms: int, corner: str, size_px: int, margin_px: int = 0) -> FlashRecord:
        """The corner patch: the flash window shrunk to a square in `corner` for
        `duration_ms`, then restored to full screen for the next flash."""
        if self.root is None:
            self.prepare()
        root = self.root
        w, h = self.size
        rect = patch_rect(corner, size_px, w, h, margin_px)
        root.geometry(f"{rect[2]}x{rect[3]}+{rect[0]}+{rect[1]}")
        try:
            root.configure(bg=color)
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.update()
            shown_qpc, shown_mono, shown_at = time.perf_counter(), time.monotonic(), _iso_now()
            deadline = shown_mono + duration_ms / 1000
            while time.monotonic() < deadline:
                root.update()
                time.sleep(0.004)
            root.withdraw()
            root.update()
        finally:
            root.geometry(f"{w}x{h}+0+0")
        hidden = time.monotonic()
        event(log, logging.INFO, "flash.patch_shown", color=color, corner=corner, rect=list(rect),
              actual_ms=round((hidden - shown_mono) * 1000, 1))
        return FlashRecord(color, duration_ms, shown_at, shown_mono, hidden, shown=True, shown_qpc=shown_qpc,
                           style="patch", rect=rect, screen=(w, h))

    def close(self) -> None:
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception as exc:  # Tk teardown after a desktop switch can fail; report only
                event(log, logging.WARNING, "flash.close_failed", error=repr(exc))
            self.root = None
