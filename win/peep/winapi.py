"""Thin ctypes wrappers for the few Win32 calls peep needs. Every function
is safe to import anywhere and degrades to a reported no-op off Windows,
so the rest of the package stays testable on Linux.

  foreground_window()   title + process image of the window the user is in
  set_dpi_aware()       per-monitor DPI awareness, so tkinter sees 2560x1600
                        rather than the 150%-scaled 1707x1067 (probed)
  KillOnCloseJob        a Job Object that kills ffmpeg if this process dies,
                        so a crashed or killed recorder never leaves an
                        orphaned ffmpeg recording forever
  pid_alive(pid)        liveness for the control file's recorder pid
  open_with_default()   os.startfile, the Windows default handler

Session B (the resident agent) adds section 5:
  SingleInstance        a named mutex, so a second agent refuses to start
  toplevel_hwnd()       the real top-level HWND behind a Tk window id
  exclude_from_capture  SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE): the
                        REC pill is seen by the user, absent from the file
                        (probe 2026-10-05: ddagrab honours it on this laptop,
                        including for the pill's layered click-through recipe)
  make_click_through    WS_EX_LAYERED|TRANSPARENT|TOOLWINDOW|NOACTIVATE
  force_foreground      bring the stop dialog to the front and give it focus
  startup_folder()      FOLDERID_Startup, for `peep agent install`

Sections:
  1. Platform + foreground      (~line 45)
  2. DPI                        (~line 103)
  3. Job object                 (~line 126)
  4. Processes + shell          (~line 199)
  5. Agent windows + lifecycle  (~line 238)
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from .logsetup import event

log = logging.getLogger("peep.winapi")

# ---------------------------------------------------------------------------
# 1. Platform + foreground
# ---------------------------------------------------------------------------


def is_windows() -> bool:
    return sys.platform == "win32"


@dataclass(frozen=True)
class ForegroundInfo:
    title: str | None
    image: str | None          # full path of the process image

    @property
    def process(self) -> str | None:
        if not self.image:
            return None
        return self.image.replace("/", "\\").rsplit("\\", 1)[-1]


def foreground_window() -> ForegroundInfo:
    """The foreground window's title and process image; empty fields (and a
    logged warning) when it cannot be read. Never raises."""
    if not is_windows():
        return ForegroundInfo(None, None)
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            event(log, logging.WARNING, "foreground.none")
            return ForegroundInfo(None, None)
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        image = None
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                img = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, img, ctypes.byref(size)):
                    image = img.value
            finally:
                kernel32.CloseHandle(handle)
        if image is None:
            event(log, logging.WARNING, "foreground.image_unreadable", pid=pid.value,
                  winerror=ctypes.get_last_error())
        return ForegroundInfo(buf.value or None, image)
    except Exception as exc:  # ctypes surface: report, never break a recording over a slug hint
        event(log, logging.WARNING, "foreground.failed", error=repr(exc))
        return ForegroundInfo(None, None)


# ---------------------------------------------------------------------------
# 2. DPI
# ---------------------------------------------------------------------------


def set_dpi_aware() -> bool:
    """Per-monitor DPI awareness for this process (call before tkinter).
    Returns True on success or if already set; logs and returns False otherwise."""
    if not is_windows():
        return False
    try:
        import ctypes
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        # E_ACCESSDENIED (0x80070005) means awareness was already set — fine.
        ok = hr in (0, -2147024891)
        if not ok:
            event(log, logging.WARNING, "dpi.set_failed", hresult=hr)
        return ok
    except Exception as exc:
        event(log, logging.WARNING, "dpi.set_failed", error=repr(exc))
        return False


# ---------------------------------------------------------------------------
# 3. Job object
# ---------------------------------------------------------------------------


class KillOnCloseJob:
    """A Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. Assign
    ffmpeg to it; when this process exits for any reason the handle closes
    and Windows terminates ffmpeg. The Matroska capture written so far
    stays playable. Off Windows, or if any call fails, `assign` returns
    False and says why in the log — recording proceeds without the net."""

    def __init__(self):
        self.handle = None
        if not is_windows():
            return
        try:
            import ctypes
            from ctypes import wintypes

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in
                            ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                             "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                            ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(wintypes.HANDLE(handle), 9, ctypes.byref(info),
                                               ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            self._k32, self.handle = k32, handle
        except Exception as exc:
            event(log, logging.WARNING, "job.create_failed", error=repr(exc))
            self.handle = None

    def assign(self, popen) -> bool:
        if self.handle is None:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            proc_handle = wintypes.HANDLE(int(popen._handle))
            if not self._k32.AssignProcessToJobObject(wintypes.HANDLE(self.handle), proc_handle):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
            event(log, logging.INFO, "job.assigned", pid=popen.pid)
            return True
        except Exception as exc:
            event(log, logging.WARNING, "job.assign_failed", pid=getattr(popen, "pid", None), error=repr(exc))
            return False


# ---------------------------------------------------------------------------
# 4. Processes + shell
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """True if a process with this pid is running. Windows: OpenProcess +
    GetExitCodeProcess == STILL_ACTIVE; elsewhere: os.kill(pid, 0)."""
    if pid <= 0:
        return False
    if not is_windows():
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def open_with_default(path: str) -> None:
    """Open a file or folder with its Windows default handler."""
    if not is_windows():
        raise OSError("opening with the default handler needs Windows (run through the peep shim)")
    os.startfile(path)  # noqa: S606 — the whole point


# ---------------------------------------------------------------------------
# 5. Agent windows + lifecycle (session B)
# ---------------------------------------------------------------------------

WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80000, 0x20, 0x80, 0x08000000
ERROR_ALREADY_EXISTS = 183


def _user32():
    import ctypes
    from ctypes import wintypes
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.GetAncestor.restype = wintypes.HWND
    u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    u.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    u.BringWindowToTop.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    u.GetForegroundWindow.restype = wintypes.HWND
    return u


class SingleInstance:
    """A named mutex held for the life of the process. `acquired` is False when
    another process already holds it. Off Windows it always acquires (the
    pidfile in control.AgentControl still guards, less strictly)."""

    def __init__(self, name: str = "Local\\peep-agent"):
        self.name, self.handle, self.acquired = name, None, True
        if not is_windows():
            return
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        self.handle = k32.CreateMutexW(None, False, name)
        err = ctypes.get_last_error()
        if not self.handle:
            event(log, logging.WARNING, "mutex.create_failed", mutex=name, winerror=err)
            return
        self.acquired = err != ERROR_ALREADY_EXISTS
        event(log, logging.INFO, "mutex.created", mutex=name, acquired=self.acquired)


def toplevel_hwnd(tk_window_id: int) -> int | None:
    """Tk's winfo_id() is a child window; capture affinity and extended styles
    belong on the top-level wrapper (GetAncestor GA_ROOT)."""
    if not is_windows():
        return None
    try:
        return _user32().GetAncestor(tk_window_id, 2) or None
    except Exception as exc:
        event(log, logging.WARNING, "hwnd.ancestor_failed", error=repr(exc))
        return None


def exclude_from_capture(hwnd: int | None) -> bool:
    """True only if the affinity reads back as WDA_EXCLUDEFROMCAPTURE. Callers
    must not show a window over a recording when this returns False."""
    if not hwnd:
        event(log, logging.WARNING, "capture_exclusion.no_hwnd")
        return False
    try:
        import ctypes
        from ctypes import wintypes
        u = _user32()
        ok = u.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
        err = ctypes.get_last_error()
        got = wintypes.DWORD()
        u.GetWindowDisplayAffinity(hwnd, ctypes.byref(got))
        result = bool(ok) and got.value == WDA_EXCLUDEFROMCAPTURE
        event(log, logging.INFO if result else logging.WARNING, "capture_exclusion.set", hwnd=hwnd,
              ok=bool(ok), winerror=err, readback=got.value)
        return result
    except Exception as exc:
        event(log, logging.WARNING, "capture_exclusion.failed", error=repr(exc))
        return False


def make_click_through(hwnd: int | None) -> bool:
    if not hwnd:
        return False
    try:
        u = _user32()
        ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        return True
    except Exception as exc:
        event(log, logging.WARNING, "click_through.failed", error=repr(exc))
        return False


def force_foreground(hwnd: int | None) -> str:
    """Put `hwnd` in front with keyboard focus. Returns which step worked
    ('direct', 'attach', 'alt-tap') or 'failed'; every attempt is logged.

    The laptop's ForegroundLockTimeout is at its maximum (probe), so Windows
    only lets a process take the foreground when it received the last input.
    The stop hotkey normally qualifies us; if the user typed elsewhere while
    the file was finalizing, the AttachThreadInput and Alt-tap fallbacks are
    the documented-in-practice ways back in."""
    if not hwnd or not is_windows():
        return "failed"
    try:
        import ctypes
        u = _user32()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if u.SetForegroundWindow(hwnd) and u.GetForegroundWindow() == hwnd:
            step = "direct"
        else:
            fg = u.GetForegroundWindow()
            fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else 0
            me = k32.GetCurrentThreadId()
            attached = bool(fg_tid and fg_tid != me and u.AttachThreadInput(me, fg_tid, True))
            try:
                u.BringWindowToTop(hwnd)
                u.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    u.AttachThreadInput(me, fg_tid, False)
            if u.GetForegroundWindow() == hwnd:
                step = "attach"
            else:
                VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x2
                u.keybd_event(VK_MENU, 0, 0, 0)
                u.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
                u.SetForegroundWindow(hwnd)
                step = "alt-tap" if u.GetForegroundWindow() == hwnd else "failed"
        event(log, logging.INFO if step != "failed" else logging.WARNING, "foreground.force", hwnd=hwnd, step=step)
        return step
    except Exception as exc:
        event(log, logging.WARNING, "foreground.force_failed", error=repr(exc))
        return "failed"


def startup_folder() -> str | None:
    """FOLDERID_Startup via SHGetKnownFolderPath (honours folder redirection);
    None off Windows or on failure (callers fall back to %APPDATA%)."""
    if not is_windows():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                        ("d", ctypes.c_ubyte * 8)]

        g = GUID(0xB97D20BB, 0xF46A, 0x4C97,
                 (ctypes.c_ubyte * 8)(0xBA, 0x10, 0x5E, 0x36, 0x08, 0x43, 0x08, 0x54))
        out = ctypes.c_wchar_p()
        hr = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
        path = out.value
        ctypes.windll.ole32.CoTaskMemFree(out)
        if hr != 0 or not path:
            event(log, logging.WARNING, "startup_folder.failed", hresult=hr)
            return None
        return path
    except Exception as exc:
        event(log, logging.WARNING, "startup_folder.failed", error=repr(exc))
        return None
