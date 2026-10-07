"""The agent's one Tk thread, and how other threads reach it (A's Proposal 1).

The agent owns exactly one Tk interpreter, on its main thread. The pill,
the toast, the stop dialog and the clapper flashes are all Toplevels of
that one root. Two other threads exist: the hotkey listener and the
recorder worker (A's `Recorder.record`, unchanged). Neither touches Tk;
they hand work to the UI thread through `UiDispatcher`:

  post(fn, *args)    fire-and-forget (hotkey presses, recorder status lines)
  call(fn, *args)    run on the UI thread and wait for the result (the flash:
                     the recorder must know the flash was on screen, and when,
                     before it carries on; A's timeline depends on that)

The dispatcher drains its queue from a Tk `after` loop and is deliberately
non-reentrant: A's flash runs `root.update()` in a loop for its 200 ms,
which fires `after` callbacks, and a nested drain there would start the
next queued job (another flash, a hotkey) in the middle of this one. A
drain that finds the dispatcher busy just reschedules itself.

`ToplevelFlasher` is A's `TkFlasher` with one change: its window is a
Toplevel of the agent's root instead of a second `tk.Tk()`. `flash()` is
inherited unchanged. `MarshalledFlasher` is what the recorder's
`flasher_factory` returns in the agent: the flasher interface, executed
on the UI thread, and never an exception into the recording. A flash that
cannot run is logged and recorded as not shown, like NullFlasher.

`TkUi` (section 3) is the agent's whole UI surface on top of these: the
pill, toasts, the stop dialog and the flasher, as the small interface
`agent.Agent` is written (and tested) against.

Sections:
  1. Dispatcher                 (~line 50)
  2. Flashers                   (~line 135)
  3. TkUi                       (~line 200)
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from .flash import FlashRecord, NullFlasher, TkFlasher
from .logsetup import event

log = logging.getLogger("peep.ui")

# ---------------------------------------------------------------------------
# 1. Dispatcher
# ---------------------------------------------------------------------------


class UiDispatcher:
    """`schedule(ms, fn)` is Tk's `root.after` in the agent; tests pass a fake
    and call `drain()` themselves."""

    def __init__(self, schedule: Callable[[int, Callable], object], poll_ms: int = 15,
                 ui_thread: threading.Thread | None = None):
        self.schedule = schedule
        self.poll_ms = poll_ms
        self.ui_thread = ui_thread or threading.current_thread()
        self._q: queue.Queue = queue.Queue()
        self._busy = False
        self._stopped = False

    def start(self) -> None:
        self.schedule(self.poll_ms, self._loop)

    def stop(self) -> None:
        self._stopped = True

    @property
    def busy(self) -> bool:
        return self._busy

    def on_ui_thread(self) -> bool:
        return threading.current_thread() is self.ui_thread

    def post(self, fn: Callable, *args) -> None:
        self._q.put((fn, args, None))

    def call(self, fn: Callable, *args, timeout_s: float = 10.0):
        """Run fn(*args) on the UI thread and return its result (or raise its
        exception). Called from the UI thread itself, it runs inline."""
        if self.on_ui_thread():
            return fn(*args)
        box: dict = {}
        done = threading.Event()
        self._q.put((fn, args, (box, done)))
        if not done.wait(timeout_s):
            raise TimeoutError(f"UI thread did not run {getattr(fn, '__name__', fn)} within {timeout_s:g}s")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def drain(self) -> int:
        """Run everything queued; returns how many jobs ran (0 when re-entered)."""
        if self._busy:
            return 0
        self._busy = True
        ran = 0
        try:
            while True:
                try:
                    fn, args, reply = self._q.get_nowait()
                except queue.Empty:
                    return ran
                ran += 1
                try:
                    result = fn(*args)
                    if reply is not None:
                        reply[0]["result"] = result
                except BaseException as exc:      # never let one job kill the UI loop; report it
                    if reply is not None:
                        reply[0]["error"] = exc
                    else:
                        log.exception("ui job %s failed", getattr(fn, "__name__", fn))
                        event(log, logging.ERROR, "ui.job_failed", job=getattr(fn, "__name__", repr(fn)),
                              error=repr(exc))
                finally:
                    if reply is not None:
                        reply[1].set()
        finally:
            self._busy = False

    def _loop(self) -> None:
        if self._stopped:
            return
        self.drain()
        self.schedule(self.poll_ms, self._loop)


# ---------------------------------------------------------------------------
# 2. Flashers
# ---------------------------------------------------------------------------


class ToplevelFlasher(TkFlasher):
    """A's TkFlasher drawn as a Toplevel of the agent's single Tk root.
    The process is already DPI-aware (the agent sets it before creating
    Tk), so the screen size here is the physical 2560x1600."""

    def __init__(self, master):
        super().__init__()
        self.master = master

    def prepare(self) -> None:
        if self.root is not None:
            return
        import tkinter as tk
        top = tk.Toplevel(self.master)
        top.withdraw()
        top.overrideredirect(True)
        w, h = top.winfo_screenwidth(), top.winfo_screenheight()
        top.geometry(f"{w}x{h}+0+0")
        top.attributes("-topmost", True)
        try:
            top.configure(cursor="none")
        except tk.TclError as exc:
            event(log, logging.INFO, "flash.cursor_hide_unsupported", error=str(exc))
        top.update_idletasks()
        self.root, self.size = top, (w, h)
        event(log, logging.INFO, "flash.prepared", width=w, height=h, toplevel=True)


class MarshalledFlasher:
    """The recorder-facing flasher in the agent. One shared ToplevelFlasher is
    prepared once and reused for every recording; `close()` therefore leaves
    it alone (the agent destroys it at exit)."""

    def __init__(self, dispatcher: UiDispatcher, shared, timeout_s: float = 5.0):
        self.dispatcher, self.shared, self.timeout_s = dispatcher, shared, timeout_s

    def prepare(self) -> None:
        # Raising here is fine: A's _prepare_flasher turns it into "recording without flash", loudly.
        self.dispatcher.call(self.shared.prepare, timeout_s=self.timeout_s)

    def flash(self, color: str, duration_ms: int) -> FlashRecord:
        try:
            return self.dispatcher.call(self.shared.flash, color, duration_ms, timeout_s=self.timeout_s)
        except Exception as exc:
            event(log, logging.ERROR, "flash.marshal_failed", color=color, error=repr(exc))
            return NullFlasher().flash(color, duration_ms)

    def patch(self, color: str, duration_ms: int, corner: str, size_px: int, margin_px: int = 0) -> FlashRecord:
        """Session C1a's corner patch, on the UI thread like the flash."""
        try:
            return self.dispatcher.call(self.shared.patch, color, duration_ms, corner, size_px, margin_px,
                                        timeout_s=self.timeout_s)
        except Exception as exc:
            event(log, logging.ERROR, "flash.marshal_failed", color=color, style="patch", error=repr(exc))
            return NullFlasher().patch(color, duration_ms, corner, size_px, margin_px)

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 3. TkUi
# ---------------------------------------------------------------------------


class TkUi:
    """The agent's UI on one Tk root (created by run_agent, after DPI awareness).

    Every method except `post` runs on the Tk thread; `every` callbacks and
    posted jobs go through the dispatcher, so agent logic is never re-entered
    from inside a flash's `update()` loop."""

    def __init__(self, root):
        from .pill import Overlay
        self.root = root
        self.dispatcher = UiDispatcher(root.after)
        self.dispatcher.start()
        self.flasher = ToplevelFlasher(root)
        self.pill_overlay = Overlay(root)
        self.toast_overlay = Overlay(root, font=("Segoe UI", 10), alpha=0.95)
        # Used only when nothing is recording, if the excluded toast could not be secured.
        self.toast_fallback = Overlay(root, font=("Segoe UI", 10), alpha=0.95, require_exclusion=False)
        self._toast_job = None

    # -- scheduling -------------------------------------------------------------

    def post(self, fn: Callable, *args) -> None:
        self.dispatcher.post(fn, *args)

    def every(self, ms: int, fn: Callable) -> None:
        def loop():
            self.dispatcher.post(fn)
            self.root.after(ms, loop)
        self.root.after(ms, loop)

    # -- overlays ---------------------------------------------------------------

    def prepare_overlays(self) -> bool:
        """Secure the pill and toast windows now; True if the pill is excluded from capture."""
        self.toast_overlay.prepare()
        return self.pill_overlay.prepare()

    def pill(self, text: str | None, color: str | None = None, position: str = "top-right",
             margin: int = 24) -> bool:
        if text is None:
            self.pill_overlay.hide()
            return True
        return self.pill_overlay.show(text, color, position, margin)

    def toast(self, text: str, color: str, seconds: float, position: str, margin: int,
              unexcluded_ok: bool) -> bool:
        stack = 1 if self.pill_overlay.visible else 0
        overlay = self.toast_overlay
        shown = overlay.show(text, color, position, margin, stack)
        if not shown and unexcluded_ok:
            overlay = self.toast_fallback
            shown = overlay.show(text, color, position, margin, stack)
        if self._toast_job is not None:
            self.root.after_cancel(self._toast_job)
        for other in (self.toast_overlay, self.toast_fallback):
            if other is not overlay:
                other.hide()
        self._toast_job = self.root.after(int(seconds * 1000), overlay.hide) if shown else None
        return shown

    # -- dialog + flash -------------------------------------------------------------

    def open_dialog(self, model, on_result):
        from . import winapi
        from .dialog import StopDialog
        return StopDialog(self.root, model, on_result, focus=winapi.force_foreground)

    def flasher_factory(self) -> MarshalledFlasher:
        return MarshalledFlasher(self.dispatcher, self.flasher)

    def quit(self) -> None:
        self.dispatcher.stop()
        self.flasher.close()
        for overlay in (self.pill_overlay, self.toast_overlay, self.toast_fallback):
            overlay.destroy()
        self.root.quit()
