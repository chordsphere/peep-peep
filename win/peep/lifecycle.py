"""The agent's lifecycle on Windows: the Startup-folder entry, launching the
agent detached, and whether the installed copy matches the repo.

Startup entry: `peep agent install` writes
`%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\peep agent.lnk`
(the real path comes from FOLDERID_Startup, which honours redirection; the
probe found it at exactly that path on the laptop), pointing at

    pythonw.exe "%LOCALAPPDATA%\\peep\\app\\peepw.py" agent run

per A's decision 7. A .lnk rather than a .cmd because a .cmd flashes a
console window at every login; the shortcut is written by PowerShell's
WScript.Shell COM object (no admin, no non-stdlib Python dependency;
probed: it creates in 0.3 s). Uninstall deletes the file.

Launching: `peep agent start` runs (through interop) a Windows python.exe
that spawns pythonw.exe detached, with no console and no inherited
handles. The probe found that the interop-launched python.exe is not in a
job object, so the child outlives it, the shim and the terminal.

Staleness: the agent runs the Windows-local copy, which only the WSL shim
refreshes. At agent start (and in `peep agent status`), if WSL is already
running, the copy's file hashes are compared with the repo over
\\\\wsl.localhost. The check never starts WSL itself: it asks `wsl.exe
--list --running` first, because touching \\\\wsl.localhost boots the
distro, which at login is exactly the wrong side effect. It never syncs
either; the shim owns that.

Sections:
  1. Startup entry              (~line 50)
  2. Launching the agent        (~line 135)
  3. Installed-copy staleness   (~line 180)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Callable

from .logsetup import event

log = logging.getLogger("peep.lifecycle")

# ---------------------------------------------------------------------------
# 1. Startup entry
# ---------------------------------------------------------------------------

LINK_NAME = "peep agent.lnk"
STARTUP_REL = r"Microsoft\Windows\Start Menu\Programs\Startup"


class LifecycleError(RuntimeError):
    """A lifecycle step that failed; the message is user-facing."""


def startup_dir(environ: dict | None = None, known_folder: Callable[[], str | None] | None = None) -> PureWindowsPath:
    """FOLDERID_Startup when Windows can tell us, else %APPDATA%\\...\\Startup."""
    env = os.environ if environ is None else environ
    if known_folder is None:
        from . import winapi
        known_folder = winapi.startup_folder
    path = known_folder()
    if path:
        return PureWindowsPath(path)
    if env.get("APPDATA"):
        return PureWindowsPath(env["APPDATA"]) / STARTUP_REL
    raise LifecycleError("cannot locate the Startup folder (no FOLDERID_Startup and no %APPDATA%)")


def app_dir() -> Path:
    """The directory holding peepw.py and the peep package: the installed copy
    (%LOCALAPPDATA%\\peep\\app) when run from there, the repo's win/ in tests."""
    return Path(__file__).resolve().parent.parent


def pythonw_for(python_exe: str) -> PureWindowsPath:
    """pythonw.exe beside the running python.exe (the probe found both in
    %LOCALAPPDATA%\\Programs\\Python\\Python312)."""
    return PureWindowsPath(python_exe).with_name("pythonw.exe")


def agent_launch(app: Path | PureWindowsPath, python_exe: str) -> tuple[str, list[str], str]:
    """(pythonw.exe, [peepw.py, 'agent', 'run'], working dir)."""
    app_w = PureWindowsPath(str(app))
    return str(pythonw_for(python_exe)), [str(app_w / "peepw.py"), "agent", "run"], str(app_w)


def ps_quote(text: str) -> str:
    """A PowerShell single-quoted literal: no expansion; ' doubles."""
    return "'" + text.replace("'", "''") + "'"


def shortcut_script(link: str, target: str, arguments: list[str], workdir: str, description: str) -> str:
    """PowerShell that writes one .lnk. Every value is a single-quoted literal,
    so paths with spaces, $ or quotes cannot be reinterpreted."""
    args = subprocess.list2cmdline(arguments)
    return ("$ErrorActionPreference='Stop'; "
            "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + ps_quote(link) + "); "
            "$s.TargetPath=" + ps_quote(target) + "; "
            "$s.Arguments=" + ps_quote(args) + "; "
            "$s.WorkingDirectory=" + ps_quote(workdir) + "; "
            "$s.Description=" + ps_quote(description) + "; "
            "$s.WindowStyle=7; $s.Save(); 'ok'")


def install_startup_entry(link_dir: PureWindowsPath, app: Path | PureWindowsPath, python_exe: str,
                          run: Callable = subprocess.run, exists: Callable[[str], bool] = os.path.exists) -> str:
    """Write the Startup shortcut; returns its path. Raises LifecycleError with the reason."""
    target, arguments, workdir = agent_launch(app, python_exe)
    if not exists(target):
        raise LifecycleError(f"{target} not found; reinstall Python with: winget install Python.Python.3.12")
    link = str(link_dir / LINK_NAME)
    script = shortcut_script(link, target, arguments, workdir, "peep-peep resident agent (hotkeys, REC pill)")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
    event(log, logging.INFO, "startup.install", link=link, target=target, arguments=arguments)
    try:
        res = run(argv, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LifecycleError(f"could not run powershell.exe to write the shortcut: {exc}") from exc
    if res.returncode != 0 or "ok" not in (res.stdout or ""):
        detail = (res.stderr or res.stdout or "").strip()[:300]
        event(log, logging.ERROR, "startup.install_failed", rc=res.returncode, detail=detail)
        raise LifecycleError(f"writing {link} failed (powershell exit {res.returncode}): {detail}")
    return link


def exists(path: str) -> bool:
    """os.path.exists, named here so `peep agent status` can be tested with a fake."""
    return os.path.exists(path)


def remove_startup_entry(link_dir: PureWindowsPath, remove: Callable[[str], None] = os.remove) -> str | None:
    """Delete the shortcut; returns its path, or None if there was none."""
    link = str(link_dir / LINK_NAME)
    try:
        remove(link)
    except FileNotFoundError:
        return None
    event(log, logging.INFO, "startup.removed", link=link)
    return link


# ---------------------------------------------------------------------------
# 2. Launching the agent
# ---------------------------------------------------------------------------

# DETACHED_PROCESS: no console (pythonw has none anyway); its own process group so a
# Ctrl-C in whatever console started `peep agent start` can never reach it.
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP = 0x8, 0x200


def spawn_agent(app: Path, python_exe: str = sys.executable, popen: Callable = subprocess.Popen,
                exists: Callable[[str], bool] = os.path.exists) -> int:
    """Start `pythonw.exe peepw.py agent run` detached; returns its pid."""
    target, arguments, workdir = agent_launch(app, python_exe)
    if not exists(target):
        raise LifecycleError(f"{target} not found; reinstall Python with: winget install Python.Python.3.12")
    argv = [target, *arguments]
    event(log, logging.INFO, "agent.spawn", argv=argv)
    kwargs = dict(cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True)
    if sys.platform == "win32":
        kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        proc = popen(argv, **kwargs)
    except OSError as exc:
        raise LifecycleError(f"could not start the agent ({target}): {exc}") from exc
    return proc.pid


# ---------------------------------------------------------------------------
# 3. Installed-copy staleness
# ---------------------------------------------------------------------------

MARKER = "INSTALLED.json"


def package_files(src: Path) -> dict[str, str]:
    """relative posix path -> sha256, over exactly the set the WSL shim syncs
    (bin/peep `source_files`): peepw.py and peep/**/*.py, caches excluded."""
    out = {}
    for p in [src / "peepw.py"] + sorted((src / "peep").rglob("*.py")):
        if "__pycache__" in p.parts or not p.exists():
            continue
        out[p.relative_to(src).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def code_id(files: dict[str, str]) -> str:
    """A short fingerprint of a file->hash map: what the agent records as the
    code it loaded, so `peep agent status` can tell a running agent is older
    than the copy on disk."""
    blob = json.dumps(files, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


def read_marker(app: Path) -> dict | None:
    try:
        return json.loads((Path(app) / MARKER).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        event(log, logging.WARNING, "install.marker_unreadable", error=str(exc))
        return None


def parse_running_distros(stdout: bytes) -> list[str]:
    """`wsl.exe --list --running --quiet` prints UTF-16LE (with NULs) on most
    builds and UTF-8 on some; one distro per line."""
    if not stdout:
        return []
    text = stdout.decode("utf-16-le", "replace") if b"\x00" in stdout else stdout.decode("utf-8", "replace")
    return [line.strip().lstrip("﻿") for line in text.splitlines() if line.strip().lstrip("﻿")]


def running_distros(run: Callable = subprocess.run) -> list[str] | None:
    """Distros currently running, or None if wsl.exe could not answer. Never starts one."""
    try:
        res = run(["wsl.exe", "--list", "--running", "--quiet"], capture_output=True, timeout=15,
                  stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        event(log, logging.INFO, "install.wsl_query_failed", error=repr(exc))
        return None
    if res.returncode != 0:
        return []                     # wsl.exe exits non-zero when nothing is running
    return parse_running_distros(res.stdout)


def repo_unc(distro: str, source: str) -> PureWindowsPath:
    """('Ubuntu', '/home/chordsphere/peep-peep/win') -> \\\\wsl.localhost\\Ubuntu\\home\\...\\win"""
    return PureWindowsPath(r"\\wsl.localhost" + "\\" + distro + source.replace("/", "\\"))


def install_status(app: Path, run: Callable = subprocess.run,
                   files_at: Callable[[Path], dict[str, str]] = package_files) -> dict:
    """{"state": "current" | "stale" | "unknown", "detail": str, "changed": [...]}. Never raises."""
    try:
        marker = read_marker(app)
        if marker is None:
            return {"state": "unknown", "detail": "no INSTALLED.json; run `peep install` from WSL"}
        installed = marker.get("files") or {}
        distro, source = marker.get("wsl_distro"), marker.get("source")
        if not distro or not source:
            return {"state": "unknown",
                    "detail": "INSTALLED.json does not name the WSL distro (written before session B); "
                              "any `peep` command from WSL refreshes it"}
        running = running_distros(run)
        if running is None:
            return {"state": "unknown", "detail": "wsl.exe did not answer; not checked"}
        if distro not in running:
            return {"state": "unknown", "detail": f"WSL ({distro}) is not running; not checked (the agent never starts it)"}
        repo = Path(str(repo_unc(distro, source)))
        if not (repo / "peepw.py").exists():
            return {"state": "unknown", "detail": f"repo not reachable at {repo}"}
        current = files_at(repo)
        changed = sorted(k for k in set(current) | set(installed) if current.get(k) != installed.get(k))
        if changed:
            return {"state": "stale", "changed": changed,
                    "detail": f"{len(changed)} file(s) differ from the repo; run any `peep` command in WSL "
                              f"(e.g. `peep agent restart`) to sync and reload"}
        return {"state": "current", "detail": f"matches {repo}"}
    except Exception as exc:      # a status line must never take the agent down
        event(log, logging.WARNING, "install.check_failed", error=repr(exc))
        return {"state": "unknown", "detail": f"check failed: {exc!r}"}
