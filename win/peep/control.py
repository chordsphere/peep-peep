"""Control files: how a running `rec` and a separate `peep stop` (or, later,
session B's agent) talk. Chosen over a 127.0.0.1 socket because it needs
no port, no listener thread, survives either side restarting, and is
inspectable with `type`/`cat` while debugging.

In `paths.state_dir()`:

  active.json      written by the recorder when capture is live, removed
                   when it has finished (successfully or not). Holds the
                   recorder's pid, so a stale file from a crashed recorder
                   is detected (pid not alive) rather than trusted.
  stop-request     written by `stop`; the recorder polls for it every
                   100 ms, consumes it, and stops cleanly.
  mark-<ns>-<pid>.json
                   one file per mark request (`peep mark`, the agent's mark
                   hotkey); the recorder polls them with the stop request,
                   flashes, and appends each to the sidecar's `marks`. One
                   file per request, so two presses inside one poll interval
                   are two marks, not one.
  event-<ns>-<pid>.json
                   session C1a: one file per take / retake / pause / resume
                   request (`peep take|retake|pause|resume`, the agent's
                   chords), same pattern as marks. Each carries the
                   requester's QPC stamp (`requested_qpc`), so the recorder
                   places the press where it happened, not where it was polled.
  agent.json       the resident agent's pid and status (AgentControl)
  agent-command    `stop` or `reload`, written by `peep agent stop|reload`

The recorder clears any stop-request and mark files left over from before
it started, so a stray `peep stop` cannot end the next recording at birth.

Sections:
  1. Recording control (Control)      (~line 45)
  2. Agent control (AgentControl)     (~line 195)
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from .catalog import now_iso, write_json_atomic
from .logsetup import event

log = logging.getLogger("peep.control")

ACTIVE = "active.json"
STOP = "stop-request"
MARK_GLOB = "mark-*.json"
EVENT_GLOB = "event-*.json"
EVENT_KINDS = ("take", "retake", "pause", "resume", "pause-toggle")
AGENT = "agent.json"
AGENT_COMMAND = "agent-command"
AGENT_COMMANDS = ("stop", "reload")


class AlreadyRecording(RuntimeError):
    pass


class AgentAlreadyRunning(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 1. Recording control (Control)
# ---------------------------------------------------------------------------


class Control:
    def __init__(self, state_dir: Path, pid_alive: Callable[[int], bool]):
        self.dir = Path(state_dir)
        self.active_path = self.dir / ACTIVE
        self.stop_path = self.dir / STOP
        self.pid_alive = pid_alive

    # -- recorder side -------------------------------------------------------

    def claim(self, info: dict) -> None:
        """Mark a recording live. Refuses if another live recorder holds it;
        replaces (and logs) a stale file whose pid is gone."""
        self.dir.mkdir(parents=True, exist_ok=True)
        current = self.read_active()
        if current is not None:
            pid = int(current.get("pid", 0))
            if self.pid_alive(pid):
                raise AlreadyRecording(f"already recording (pid {pid}): {current.get('capture')}")
            event(log, logging.WARNING, "control.stale_active", pid=pid, capture=current.get("capture"))
        self.clear_stop()
        self.clear_marks()
        write_json_atomic(self.active_path, {**info, "pid": os.getpid(), "since": now_iso()})
        event(log, logging.INFO, "control.claimed", capture=info.get("capture"))

    def release(self) -> None:
        for p in (self.active_path, self.stop_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self.clear_marks()
        event(log, logging.INFO, "control.released")

    def update_active(self, **fields) -> None:
        """Merge `fields` (e.g. status="stopping") into our own active.json.
        A file owned by another pid is never touched."""
        current = self.read_active()
        if current is None or int(current.get("pid", 0)) != os.getpid():
            event(log, logging.WARNING, "control.update_not_owner", fields=fields)
            return
        write_json_atomic(self.active_path, {**current, **fields})

    def take_marks(self) -> list[dict]:
        """Consume every pending mark request, oldest first. An unreadable
        request is logged and dropped rather than retried forever."""
        out = []
        for p in sorted(self.dir.glob(MARK_GLOB)):
            try:
                with p.open(encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, json.JSONDecodeError) as exc:
                event(log, logging.WARNING, "control.mark_unreadable", path=str(p), error=str(exc))
            try:
                p.unlink()
            except OSError as exc:
                event(log, logging.WARNING, "control.mark_unlink_failed", path=str(p), error=str(exc))
        if out:
            event(log, logging.INFO, "control.marks_consumed", count=len(out))
        return out

    def take_requests(self) -> list[dict]:
        """Consume every pending mark and event request, in press order (the
        requester's time_ns is in each file name). Marks come back with
        kind "mark". Unreadable requests are logged and dropped."""
        files = list(self.dir.glob(MARK_GLOB)) + list(self.dir.glob(EVENT_GLOB))

        def order(p: Path):
            parts = p.name.split("-")
            return (parts[1] if len(parts) > 2 else "", p.name)

        out = []
        for p in sorted(files, key=order):
            try:
                with p.open(encoding="utf-8") as fh:
                    req = json.load(fh)
                req.setdefault("kind", "mark" if p.name.startswith("mark-") else None)
                out.append(req)
            except (OSError, json.JSONDecodeError) as exc:
                event(log, logging.WARNING, "control.request_unreadable", path=str(p), error=str(exc))
            try:
                p.unlink()
            except OSError as exc:
                event(log, logging.WARNING, "control.request_unlink_failed", path=str(p), error=str(exc))
        if out:
            event(log, logging.INFO, "control.requests_consumed", count=len(out),
                  kinds=[r.get("kind") for r in out])
        return out

    def clear_marks(self) -> None:
        """Drop leftover mark and event requests (at claim and release)."""
        if not self.dir.exists():
            return
        for p in list(self.dir.glob(MARK_GLOB)) + list(self.dir.glob(EVENT_GLOB)):
            try:
                p.unlink()
            except FileNotFoundError:
                pass

    def stop_requested(self) -> bool:
        if self.stop_path.exists():
            self.clear_stop()
            event(log, logging.INFO, "control.stop_request_consumed")
            return True
        return False

    def clear_stop(self) -> None:
        try:
            self.stop_path.unlink()
        except FileNotFoundError:
            pass

    # -- client side ---------------------------------------------------------

    def read_active(self) -> dict | None:
        try:
            with self.active_path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            event(log, logging.WARNING, "control.active_unreadable", error=str(exc))
            return {"pid": 0, "capture": None, "unreadable": str(exc)}

    def live_recording(self) -> dict | None:
        """The active recording if its recorder is alive, else None."""
        info = self.read_active()
        if info and self.pid_alive(int(info.get("pid", 0))):
            return info
        return None

    def request_stop(self) -> dict:
        """Ask the live recorder to stop. Raises LookupError if none is live."""
        info = self.live_recording()
        if info is None:
            raise LookupError("nothing is recording")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stop_path.write_text(now_iso() + "\n", encoding="utf-8")
        event(log, logging.INFO, "control.stop_requested", pid=info.get("pid"))
        return info

    def request_mark(self, source: str, label: str = "") -> dict:
        """Ask the live recorder to drop a mark. Raises LookupError if none is live.
        Returns the request as written (requested_at is this side's wall clock)."""
        info = self.live_recording()
        if info is None:
            raise LookupError("nothing is recording")
        self.dir.mkdir(parents=True, exist_ok=True)
        req = {"requested_at": now_iso(), "requested_qpc": time.perf_counter(), "source": source, "label": label}
        name = f"mark-{time.time_ns():020d}-{os.getpid()}.json"
        write_json_atomic(self.dir / name, req)      # temp + replace: the poller never sees half a file
        event(log, logging.INFO, "control.mark_requested", source=source, label=label, pid=info.get("pid"))
        return req

    def request_event(self, kind: str, source: str, *, qpc: float | None = None, at: str | None = None) -> dict:
        """Ask the live recorder for a take / retake / pause / resume / pause-toggle
        (session C1a). Raises LookupError if none is live. `qpc`/`at` are the
        press instant when the caller stamped it earlier (the agent stamps the
        hotkey the moment it arrives); otherwise now."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event {kind!r} (known: {', '.join(EVENT_KINDS)})")
        info = self.live_recording()
        if info is None:
            raise LookupError("nothing is recording")
        self.dir.mkdir(parents=True, exist_ok=True)
        req = {"kind": kind, "requested_at": at or now_iso(),
               "requested_qpc": qpc if qpc is not None else time.perf_counter(), "source": source}
        name = f"event-{time.time_ns():020d}-{os.getpid()}.json"
        write_json_atomic(self.dir / name, req)
        event(log, logging.INFO, "control.event_requested", kind=kind, source=source, pid=info.get("pid"))
        return {**req, "pid": info.get("pid"), "file": name}

    def wait_status(self, pid: int, statuses: tuple[str, ...], timeout_s: float, poll_s: float = 0.1,
                    sleep: Callable[[float], None] = time.sleep,
                    clock: Callable[[], float] = time.monotonic) -> dict | None:
        """Wait until recorder `pid`'s active.json shows one of `statuses`; the info,
        or None on timeout or when that recorder has gone (`peep pause` waits for
        "paused", `peep resume` for "recording")."""
        deadline = clock() + timeout_s
        while True:
            info = self.read_active()
            if info is None or int(info.get("pid", 0)) != pid or not self.pid_alive(pid):
                return None
            if info.get("status") in statuses:
                return info
            if clock() >= deadline:
                return None
            sleep(poll_s)

    def wait_finished(self, pid: int, timeout_s: float, poll_s: float = 0.2,
                      sleep: Callable[[float], None] = time.sleep,
                      clock: Callable[[], float] = time.monotonic) -> bool:
        """Wait until the recorder `pid` has released active.json (or died)."""
        deadline = clock() + timeout_s
        while clock() < deadline:
            info = self.read_active()
            if info is None or int(info.get("pid", 0)) != pid or not self.pid_alive(pid):
                return True
            sleep(poll_s)
        return False


# ---------------------------------------------------------------------------
# 2. Agent control (AgentControl)
# ---------------------------------------------------------------------------


class AgentControl:
    """The resident agent's pidfile and command file, same pattern as Control.

    The Windows named mutex (winapi.SingleInstance) is what actually keeps a
    second agent out, race-free; agent.json is what `peep agent status|stop|
    reload` read, and what a second agent names in its refusal."""

    def __init__(self, state_dir: Path, pid_alive: Callable[[int], bool]):
        self.dir = Path(state_dir)
        self.path = self.dir / AGENT
        self.command_path = self.dir / AGENT_COMMAND
        self.pid_alive = pid_alive

    def read(self) -> dict | None:
        try:
            with self.path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            event(log, logging.WARNING, "agent.pidfile_unreadable", error=str(exc))
            return {"pid": 0, "unreadable": str(exc)}

    def live(self) -> dict | None:
        info = self.read()
        if info and self.pid_alive(int(info.get("pid", 0))):
            return info
        return None

    # -- agent side --------------------------------------------------------------

    def claim(self, info: dict) -> None:
        """Record this process as the agent. Refuses if another live agent holds
        the file; replaces (and logs) a stale one. Clears a leftover command."""
        self.dir.mkdir(parents=True, exist_ok=True)
        current = self.read()
        if current is not None and int(current.get("pid", 0)) != os.getpid():
            pid = int(current.get("pid", 0))
            if self.pid_alive(pid):
                raise AgentAlreadyRunning(f"the peep agent is already running (pid {pid})")
            event(log, logging.WARNING, "agent.stale_pidfile", pid=pid)
        self._clear_command()
        write_json_atomic(self.path, {**info, "pid": os.getpid(), "since": now_iso()})
        event(log, logging.INFO, "agent.claimed", pid=os.getpid())

    def update(self, **fields) -> None:
        current = self.read() or {}
        if int(current.get("pid", 0)) != os.getpid():
            event(log, logging.WARNING, "agent.update_not_owner", fields=list(fields))
            return
        write_json_atomic(self.path, {**current, **fields})

    def release(self) -> None:
        current = self.read()
        if current is not None and int(current.get("pid", 0)) == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._clear_command()
        event(log, logging.INFO, "agent.released")

    def take_command(self) -> str | None:
        """The pending command (consumed), or None. Unknown words are logged and dropped."""
        try:
            text = self.command_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except OSError as exc:
            event(log, logging.WARNING, "agent.command_unreadable", error=str(exc))
            return None
        self._clear_command()
        if text not in AGENT_COMMANDS:
            event(log, logging.WARNING, "agent.command_unknown", command=text)
            return None
        event(log, logging.INFO, "agent.command", command=text)
        return text

    def _clear_command(self) -> None:
        try:
            self.command_path.unlink()
        except FileNotFoundError:
            pass

    # -- client side (peep agent stop|reload) -------------------------------------

    def send(self, command: str) -> dict:
        if command not in AGENT_COMMANDS:
            raise ValueError(f"unknown agent command {command!r}")
        info = self.live()
        if info is None:
            raise LookupError("the peep agent is not running")
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.command_path.with_name(self.command_path.name + ".tmp")
        tmp.write_text(command + "\n", encoding="utf-8")
        os.replace(tmp, self.command_path)
        event(log, logging.INFO, "agent.command_sent", command=command, pid=info.get("pid"))
        return info

    def wait_gone(self, pid: int, timeout_s: float, poll_s: float = 0.2,
                  sleep: Callable[[float], None] = time.sleep,
                  clock: Callable[[], float] = time.monotonic) -> bool:
        deadline = clock() + timeout_s
        while clock() < deadline:
            if not self.pid_alive(pid):
                return True
            sleep(poll_s)
        return not self.pid_alive(pid)

    def wait_for(self, predicate: Callable[[dict], bool], timeout_s: float, poll_s: float = 0.2,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> dict | None:
        """Poll agent.json until `predicate(info)` holds for a live agent; the info, or None on timeout."""
        deadline = clock() + timeout_s
        while True:
            info = self.live()
            if info is not None and predicate(info):
                return info
            if clock() >= deadline:
                return None
            sleep(poll_s)
