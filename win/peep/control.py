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

The recorder clears any stop-request left over from before it started,
so a stray `peep stop` cannot end the next recording at birth.
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


class AlreadyRecording(RuntimeError):
    pass


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
        write_json_atomic(self.active_path, {**info, "pid": os.getpid(), "since": now_iso()})
        event(log, logging.INFO, "control.claimed", capture=info.get("capture"))

    def release(self) -> None:
        for p in (self.active_path, self.stop_path):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        event(log, logging.INFO, "control.released")

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
