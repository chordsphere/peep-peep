"""The agent's live sampler (session C1d, auto-takes): a small thumbnail of the
captured screen a few times a second, matched against the screens learned for
this recording, turned into "appeared" / "went away" events for the recorder.

  GdiGrabber   the screen -> a 64x40 grey thumbnail through GDI: StretchBlt
               with HALFTONE from the screen DC into a tiny 32-bit DIB (ctypes,
               physical pixels: the agent is DPI-aware). The probe (2026-10-08)
               measured 33 ms wall / ~13 ms CPU per sample on the laptop, the
               same at 32x20 or 128x80 (reading the screen is the cost), and
               4 Hz added no measurable system load. It found, on this laptop:
                 - GDI honours WDA_EXCLUDEFROMCAPTURE exactly as ddagrab does
                   (a plain window and the pill's own layered recipe, both
                   absent), so the pill, the toasts and the dialog never reach
                   a sample, and nothing needs masking;
                 - GDI without CAPTUREBLT still sees layered windows, and the
                   cursor never flickered, with or without it: SRCCOPY only;
                 - GDI and ddagrab see the same frame of a hardware-decoded
                   browser video (mad 1.2-1.5 between successive grabs of each,
                   identical content), so pages and ordinary video sample as
                   recorded. DRM-protected video was not tried (see README).
  Sampler      the thread. It samples only while a recording is live and not
               paused AND at least one screen is learned; otherwise it waits
               on a condition and costs nothing. A learn press's capture
               ("wait up to ~1 s for the screen to be stable") runs on the
               same thread, so one grabber serves both and the Tk thread
               never blocks.

The decisions about takes are not here: the sampler reports what it saw
(screens.Presence: hysteresis, the time of the first sample of the run, the
score), and the recorder's event model decides with the rules table.

Testable on WSL: the grabber is injected (tests use synthetic thumbnails) and
`step()` runs one iteration synchronously.

Sections:
  1. GdiGrabber                 (~line 50)
  2. Sampler                    (~line 140)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from . import screens
from .logsetup import event

log = logging.getLogger("peep.sampler")

# ---------------------------------------------------------------------------
# 1. GdiGrabber
# ---------------------------------------------------------------------------

SRCCOPY, HALFTONE = 0x00CC0020, 4


class GdiGrabber:
    """The primary screen, averaged down to a grey thumbnail. Windows only:
    constructing it elsewhere raises OSError (the agent then reports that
    auto-takes cannot sample here). The primary screen is ddagrab's output 0
    on this single-monitor laptop; another `video.output_idx` is reported."""

    def __init__(self, width: int = screens.THUMB_WIDTH):
        import sys
        if sys.platform != "win32":
            raise OSError("screen sampling needs Windows (GDI)")
        import ctypes
        from ctypes import wintypes as W
        self._ct = ctypes
        u = ctypes.WinDLL("user32", use_last_error=True)
        g = ctypes.WinDLL("gdi32", use_last_error=True)
        u.GetDC.restype, u.GetDC.argtypes = W.HDC, [W.HWND]
        u.ReleaseDC.argtypes = [W.HWND, W.HDC]
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        g.CreateCompatibleDC.restype, g.CreateCompatibleDC.argtypes = W.HDC, [W.HDC]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                        ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                        ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                        ("biClrImportant", W.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", W.DWORD * 3)]

        g.CreateDIBSection.restype = W.HBITMAP
        g.CreateDIBSection.argtypes = [W.HDC, ctypes.POINTER(BITMAPINFO), W.UINT, ctypes.POINTER(ctypes.c_void_p),
                                       W.HANDLE, W.DWORD]
        g.SelectObject.restype, g.SelectObject.argtypes = W.HGDIOBJ, [W.HDC, W.HGDIOBJ]
        g.SetStretchBltMode.argtypes = [W.HDC, ctypes.c_int]
        g.SetBrushOrgEx.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        g.StretchBlt.argtypes = [W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 W.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, W.DWORD]
        g.DeleteObject.argtypes = [W.HGDIOBJ]
        g.DeleteDC.argtypes = [W.HDC]
        self._u, self._g = u, g
        self.screen_w, self.screen_h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        self.w, self.h = screens.thumb_size(self.screen_w, self.screen_h, width)
        self._screen = u.GetDC(None)
        if not self._screen:
            raise OSError(ctypes.get_last_error(), "GetDC(NULL) failed")
        self._mem = g.CreateCompatibleDC(self._screen)
        bmi = BITMAPINFO()
        hd = bmi.bmiHeader
        hd.biSize, hd.biWidth, hd.biHeight = ctypes.sizeof(BITMAPINFOHEADER), self.w, -self.h   # top-down
        hd.biPlanes, hd.biBitCount, hd.biCompression = 1, 32, 0
        self._bits = ctypes.c_void_p()
        self._dib = g.CreateDIBSection(self._screen, ctypes.byref(bmi), 0, ctypes.byref(self._bits), None, 0)
        if not self._mem or not self._dib or not self._bits:
            err = ctypes.get_last_error()
            self.close()
            raise OSError(err, "CreateCompatibleDC / CreateDIBSection failed")
        self._old = g.SelectObject(self._mem, self._dib)
        g.SetStretchBltMode(self._mem, HALFTONE)
        g.SetBrushOrgEx(self._mem, 0, 0, None)
        event(log, logging.INFO, "sampler.grabber", screen=f"{self.screen_w}x{self.screen_h}",
              thumb=f"{self.w}x{self.h}")

    @property
    def size(self) -> tuple[int, int]:
        return self.w, self.h

    def grab(self) -> bytes:
        ok = self._g.StretchBlt(self._mem, 0, 0, self.w, self.h, self._screen, 0, 0, self.screen_w, self.screen_h,
                                SRCCOPY)
        if not ok:
            raise OSError(self._ct.get_last_error(), "StretchBlt failed")
        self._g.GdiFlush()
        return screens.grey_from_bgra(self._ct.string_at(self._bits, self.w * self.h * 4))

    def close(self) -> None:
        try:
            if getattr(self, "_dib", None):
                if getattr(self, "_old", None):
                    self._g.SelectObject(self._mem, self._old)
                self._g.DeleteObject(self._dib)
                self._dib = None
            if getattr(self, "_mem", None):
                self._g.DeleteDC(self._mem)
                self._mem = None
            if getattr(self, "_screen", None):
                self._u.ReleaseDC(None, self._screen)
                self._screen = None
        except Exception as exc:          # ctypes surface at teardown: report, never raise from close
            event(log, logging.WARNING, "sampler.grabber_close_failed", error=repr(exc))


# ---------------------------------------------------------------------------
# 2. Sampler
# ---------------------------------------------------------------------------


class Sampler:
    """One learned screen per kind at most; `emit(kind, ref, change)` is called
    (on the sampler's thread) for each certain appearance or disappearance,
    `change` being screens.Presence's dict plus nothing else.

    Thread-safe controls, from the agent's Tk thread:
      set_running(bool)        a recording is live and capturing (not paused)
      activate(kind, ref, thumb, present)   start matching a learned screen
      drop(kind) / clear()     stop matching one / all (forgotten, recording over)
      capture(done)            a learn press: done(thumb, size, stable, waited_s, error)
                               is called on the sampler thread once the screen
                               held still (at most learn_wait_s)
      stats()                  for agent.json: running, sampling, samples, CPU cost
      stop()                   end the thread (agent exit)"""

    def __init__(self, grabber_factory: Callable[[int], object], emit: Callable[[str, str, dict], None], *,
                 thresholds: screens.Thresholds | None = None, hz: float = 4.0, learn_wait_s: float = 1.0,
                 width: int = screens.THUMB_WIDTH,
                 clock: Callable[[], float] = time.perf_counter, cpu: Callable[[], float] = time.process_time,
                 threaded: bool = True, sleep: Callable[[float], None] | None = None):
        self.grabber_factory, self.emit = grabber_factory, emit
        self.threaded = threaded           # False (tests): no thread; the caller drives step()
        self.sleep = sleep                 # the learn capture's wait between samples (None: time.sleep)
        self.thr = thresholds or screens.Thresholds()
        self.hz, self.learn_wait_s, self.width = hz, learn_wait_s, width
        self._mismatched: dict[str, str] = {}         # kind -> ref whose size no longer matches the screen
        self.clock, self.cpu = clock, cpu
        self._cond = threading.Condition()
        self._refs: dict[str, dict] = {}            # kind -> {"ref", "thumb", "presence"}
        self._running = False
        self._captures: list[Callable] = []
        self._stopped = False
        self._thread: threading.Thread | None = None
        self._grabber = None
        self.samples = 0
        self.cpu_s = 0.0
        self.errors = 0
        self.last_error: str | None = None

    # -- controls ---------------------------------------------------------------

    def configure(self, thresholds: screens.Thresholds, hz: float, learn_wait_s: float,
                  width: int | None = None) -> None:
        """New config (agent reload): applies to references learned from now on and
        to the sampling rate at once; an active reference keeps the thresholds and
        the thumbnail size it was learned with (they are in the sidecar)."""
        with self._cond:
            self.thr, self.hz, self.learn_wait_s = thresholds, hz, learn_wait_s
            if width:
                self.width = width
            self._cond.notify_all()

    def set_running(self, running: bool) -> None:
        with self._cond:
            if running != self._running:
                self._running = running
                event(log, logging.INFO, "sampler.running", running=running, screens=sorted(self._refs))
                self._cond.notify_all()

    def activate(self, kind: str, ref: str, thumb: bytes, present: bool = True,
                 thresholds: screens.Thresholds | None = None, size: tuple | None = None) -> None:
        with self._cond:
            cur = self._refs.get(kind)
            if cur is not None and cur["ref"] == ref:
                return
            self._refs[kind] = {"ref": ref, "thumb": thumb, "size": tuple(size) if size else None,
                                "presence": screens.Presence(thumb, thresholds or self.thr, present=present)}
            self._mismatched.pop(kind, None)
            event(log, logging.INFO, "sampler.activate", screen=kind, ref=ref, present=present)
            self._cond.notify_all()

    def drop(self, kind: str) -> None:
        with self._cond:
            self._mismatched.pop(kind, None)
            if self._refs.pop(kind, None) is not None:
                event(log, logging.INFO, "sampler.drop", screen=kind)

    def clear(self) -> None:
        with self._cond:
            if self._refs:
                event(log, logging.INFO, "sampler.clear", screens=sorted(self._refs))
            self._refs.clear()
            self._mismatched.clear()

    def active(self) -> dict:
        """{kind: {"ref", "thumb", "present"}} (a copy)."""
        with self._cond:
            return {k: {"ref": v["ref"], "thumb": v["thumb"], "present": v["presence"].present}
                    for k, v in self._refs.items()}

    def capture(self, done: Callable) -> None:
        with self._cond:
            self._captures.append(done)
            self._cond.notify_all()
        self._ensure_thread()

    def start(self) -> None:
        self._ensure_thread()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
        self._close_grabber()

    @property
    def sampling(self) -> bool:
        with self._cond:
            return self._running and bool(self._refs) and not self._stopped

    def stats(self) -> dict:
        with self._cond:
            return {"running": self._running, "sampling": self._running and bool(self._refs),
                    "screens": sorted(self._refs), "mismatched": sorted(self._mismatched), "hz": self.hz,
                    "samples": self.samples,
                    "cpu_ms_per_sample": round(1000 * self.cpu_s / self.samples, 1) if self.samples else None,
                    "errors": self.errors, "last_error": self.last_error}

    # -- the thread -----------------------------------------------------------------

    def _ensure_thread(self) -> None:
        if not self.threaded:
            return
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, name="peep-sampler", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        event(log, logging.INFO, "sampler.thread_start")
        next_at = None
        while True:
            with self._cond:
                while True:
                    if self._stopped:
                        break
                    if self._captures:
                        break
                    if self._running and self._refs:
                        now = self.clock()
                        if next_at is None or now >= next_at:
                            break
                        self._cond.wait(timeout=next_at - now)
                    else:
                        next_at = None                 # idle: nothing to sample, no CPU, until told
                        self._cond.wait()
                if self._stopped:
                    break
                period = 1.0 / max(0.1, self.hz)
            self.step()
            now = self.clock()
            next_at = max((next_at if next_at is not None else now) + period, now)
        self._close_grabber()
        event(log, logging.INFO, "sampler.thread_exit", samples=self.samples)

    def _grab(self) -> bytes:
        """One thumbnail, at the width the active references were learned with (else the
        configured one): a config reload never strands a learned screen."""
        with self._cond:
            width = next((r["size"][0] for r in self._refs.values() if r.get("size")), self.width)
        if self._grabber is not None and tuple(self._grabber.size)[0] != width:
            self._close_grabber()
        if self._grabber is None:
            self._grabber = self.grabber_factory(width)
        return self._grabber.grab()

    def _close_grabber(self) -> None:
        g, self._grabber = self._grabber, None
        if g is not None and hasattr(g, "close"):
            g.close()

    def _failed(self, exc: BaseException) -> None:
        self.errors += 1
        self.last_error = repr(exc)
        lvl = logging.ERROR if self.errors <= 3 or self.errors % 100 == 0 else logging.DEBUG
        event(log, lvl, "sampler.grab_failed", error=repr(exc), errors=self.errors)
        self._close_grabber()                 # a stale DC after a display change: start over

    def step(self) -> list:
        """One iteration, on the calling thread: a pending learn capture first,
        else one sample matched against every active screen. Returns the changes
        it emitted (tests drive it directly)."""
        with self._cond:
            done = self._captures.pop(0) if self._captures else None
            refs = dict(self._refs) if (self._running and not done) else {}
            thr, wait_s = self.thr, self.learn_wait_s
        if done is not None:
            self._learn_capture(done, thr, wait_s)
            return []
        if not refs:
            return []
        c0 = self.cpu()
        try:
            qpc = self.clock()
            thumb = self._grab()
        except Exception as exc:              # GDI can fail (display change, secure desktop): reported, retried
            self._failed(exc)
            return []
        out = []
        for kind, r in refs.items():
            if len(r["thumb"]) != len(thumb):
                # The screen's shape changed since it was learned (resolution, scaling): it can never
                # match again. Said once (stats -> the agent's toast), not every sample.
                with self._cond:
                    first = self._mismatched.get(kind) != r["ref"]
                    self._mismatched[kind] = r["ref"]
                if first:
                    event(log, logging.WARNING, "sampler.size_mismatch", screen=kind, ref=r["ref"],
                          reference=len(r["thumb"]), sample=len(thumb))
                continue
            change = r["presence"].feed(qpc, thumb)
            if change is not None:
                out.append((kind, r["ref"], change))
        # A start screen giving way to the end screen within one sample: the end screen's arrival is
        # decided first, so no take opens on it (screens.CHANGE_ORDER; the review's finding).
        out.sort(key=lambda o: screens.change_order(o[0], o[2]["change"]))
        self.samples += 1
        self.cpu_s += max(0.0, self.cpu() - c0)
        if self.samples % 2400 == 0:          # every ~10 minutes at 4 Hz: what sampling costs here
            event(log, logging.INFO, "sampler.cost", samples=self.samples,
                  cpu_ms_per_sample=round(1000 * self.cpu_s / self.samples, 1))
        for kind, ref, change in out:
            event(log, logging.INFO, "sampler.change", screen=kind, ref=ref, change=change["change"],
                  qpc=round(change["qpc"], 6), score=change["score"])
            try:
                self.emit(kind, ref, change)
            except Exception as exc:          # the recording may have ended between the sample and now
                event(log, logging.WARNING, "sampler.emit_failed", screen=kind, change=change["change"],
                      error=repr(exc))
        return out

    def _learn_capture(self, done: Callable, thr: screens.Thresholds, wait_s: float) -> None:
        try:
            thumb, stable, waited = screens.stable_capture(self._grab, thr, wait_s=wait_s, sleep=self.sleep)
            size = tuple(self._grabber.size)
            if len(thumb) != size[0] * size[1]:
                raise ValueError(f"the grabber returned {len(thumb)} pixels for {size[0]}x{size[1]}")
        except Exception as exc:              # reported to the learn press too: the pill says it failed
            self._failed(exc)
            done(None, None, False, 0.0, repr(exc))
            return
        event(log, logging.INFO, "sampler.learn_capture", stable=stable, waited_s=waited, size=list(size))
        done(thumb, size, stable, waited, None)
