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

Sections:
  1. Platform + foreground      (~line 33)
  2. DPI                        (~line 91)
  3. Job object                 (~line 114)
  4. Processes + shell          (~line 187)
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
