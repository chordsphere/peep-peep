"""The resident agent (`peep agent run`, launched by pythonw.exe at login):
global hotkeys, the REC pill, the stop dialog, marks, and its own lifecycle.

  Ctrl+Alt+R   nothing recording: start (foreground window read at the press,
               last-used collection). Recording: stop, then the naming dialog.
  Ctrl+Alt+M   drop a mark: a cyan flash and an entry in the sidecar's `marks`
  Ctrl+Alt+X   stop and delete the current take (catalogued as discarded)
  Ctrl+Alt+P   hard pause / resume (session C1a): capture stops, the next press opens a new segment
  Ctrl+Alt+T   take: open one, or close the open one (a blue / red corner patch)
  Ctrl+Alt+Backspace
               retake: discard the most recent take, open a fresh one (a yellow corner patch)
               (all three, like mark, act on any live recording through control files;
               the recorder decides them in events.EventModel, debounce included)

Threads: the Tk thread runs everything in this class (through ui.post /
ui.every, serialised by the UI dispatcher); the hotkey listener thread
only forwards presses; one worker thread per recording runs A's
`Recorder.record()` unchanged, with the agent's marshalled flasher and a
`stop_event` the hotkeys set (A's Proposal 1).

Terminal-driven recordings keep A's behaviour: `peep rec` + q and `peep
stop` never show the dialog. The agent does notice them (the pill shows
any live recording, from active.json), and its hotkeys act on them the way
`peep stop` / `peep mark` would: record-hotkey requests a stop, mark works
as for its own recordings, discard requests a stop and then deletes the
take. The dialog is only for recordings the agent itself started.

Everything here is testable on WSL: the UI, the recorder, the hotkey
listener and the clock are injected (tests/test_agent.py). The real Tk
wiring is `ui.TkUi`; `run_agent` puts the pieces together.

Sections:
  1. Prefs (last-used collection)   (~line 55)
  2. Agent                          (~line 100)
  3. run_agent                      (~line 470)
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__, catalog, config as config_mod, dialog, events, hotkeys, lifecycle, naming, paths
from .catalog import write_json_atomic
from .control import AgentAlreadyRunning, AgentControl, AlreadyRecording, Control
from .logsetup import event
from .pill import COLORS, pill_text
from .recorder import RecordError, RecordRequest
from .winapi import ForegroundInfo

log = logging.getLogger("peep.agent")

# ---------------------------------------------------------------------------
# 1. Prefs (last-used collection)
# ---------------------------------------------------------------------------

PREFS_NAME = "agent-prefs.json"


class Prefs:
    """What the agent remembers between runs: today, only the last-used
    collection, which a hotkey recording starts in. A missing or unreadable
    file means "none yet" (logged when unreadable); an invalid stored name
    is ignored with a warning rather than trusted."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            event(log, logging.WARNING, "prefs.unreadable", path=str(self.path), error=str(exc))
            return {}

    def last_collection(self) -> str | None:
        value = self._read().get("last_collection")
        if value is None:
            return None
        try:
            return naming.validate_collection(value)
        except (naming.NamingError, AttributeError):
            event(log, logging.WARNING, "prefs.bad_collection", value=value)
            return None

    def set_last_collection(self, collection: str) -> None:
        data = self._read()
        if data.get("last_collection") == collection:
            return
        data["last_collection"] = collection
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.path, data)
            event(log, logging.INFO, "prefs.last_collection", collection=collection)
        except OSError as exc:
            event(log, logging.WARNING, "prefs.write_failed", path=str(self.path), error=str(exc))


# ---------------------------------------------------------------------------
# 2. Agent
# ---------------------------------------------------------------------------

TICK_MS = 250
CONFIG_CHECK_EVERY = 8          # ticks: the config file's mtime is checked every 2 s
STOP_REASONS = {"stop": "hotkey", "discard": "hotkey-discard", "exit": "agent-exit"}
# Session C1a: hotkey action -> the control-file request it writes (the pause chord toggles).
EVENT_ACTIONS = {"pause": "pause-toggle", "take": "take", "retake": "retake"}
PILL_STATES = ("starting", "recording", "pausing", "paused", "resuming", "stopping")


@dataclass
class Session:
    """One recording the agent started."""
    request: RecordRequest
    collection: str
    stop_event: threading.Event
    stop_kind: str | None = None      # stop | discard | exit, once a stop was asked for


def _thread(fn: Callable, *args) -> threading.Thread:
    t = threading.Thread(target=fn, args=args, name=getattr(fn, "__name__", "agent-worker"), daemon=True)
    t.start()
    return t


def _parse_iso(text) -> _dt.datetime | None:
    try:
        return _dt.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


class Agent:
    def __init__(self, cfg: config_mod.Config, *, ui, control: Control, agent_control: AgentControl, prefs: Prefs,
                 recorder_factory: Callable, listener_factory: Callable,
                 load_config: Callable[[], config_mod.Config] | None = None,
                 config_mtime: Callable[[], float | None] | None = None,
                 install_check: Callable[[], dict] | None = None, code: str | None = None,
                 spawn: Callable = _thread, now: Callable[[], _dt.datetime] | None = None):
        self.cfg = cfg
        self.ui = ui
        self.control, self.agent_control, self.prefs = control, agent_control, prefs
        self.recorder_factory, self.listener_factory = recorder_factory, listener_factory
        self.load_config = load_config or config_mod.load
        self.config_mtime = config_mtime or (lambda: None)
        self.install_check = install_check
        self.code = code
        self.spawn = spawn
        self.now = now or (lambda: _dt.datetime.now().astimezone())
        self.session: Session | None = None
        self.dialog = None
        self.listener = None
        self.exiting = False
        self.quit_done = False
        self.pill_problem: str | None = None
        self.install: dict = {"state": "unknown", "detail": "not checked yet"}
        self._ticks = 0
        self._mtime = self.config_mtime()
        self.config_loaded_at = catalog.now_iso()

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        self._start_listener()
        if self.cfg.agent.pill and not self.ui.prepare_overlays():
            self._pill_failed()
        self.ui.every(TICK_MS, self.tick)
        if self.install_check is not None:
            self.spawn(self._check_install)
        self._publish(state="ready")
        event(log, logging.INFO, "agent.ready", version=__version__, config=self.cfg.source,
              hotkeys=self.listener.status() if self.listener else {})

    def _start_listener(self) -> None:
        table = hotkeys.table_from_agent_config(self.cfg.agent)
        self.listener = self.listener_factory(table, self.on_hotkey, self._on_problem_thread,
                                              self._on_registered_thread, self.cfg.agent.hotkey_retry_s)
        self.listener.start()

    def shutdown(self, reason: str) -> None:
        """Finish cleanly: a dialog keeps its automatic name, a recording is
        stopped and finalized (no dialog), then hotkeys go and the UI closes."""
        if self.exiting:
            return
        self.exiting = True
        event(log, logging.INFO, "agent.shutdown", reason=reason, recording=self.session is not None)
        self._publish(state="stopping")
        if self.dialog is not None and not self.dialog.closed:
            self.dialog.resolve(dialog.KEEP)
        if self.session is not None:
            self._stop_own("exit")
            return                          # _finished completes the shutdown
        self._quit()

    def _quit(self) -> None:
        if self.quit_done:
            return
        self.quit_done = True
        if self.listener is not None:
            self.listener.stop()
        self.agent_control.release()
        event(log, logging.INFO, "agent.exit")
        self.ui.quit()

    # -- hotkeys (listener thread -> UI thread) ---------------------------------------

    def on_hotkey(self, action: str, foreground: ForegroundInfo | None) -> None:
        # Stamped here, on the listener thread the instant WM_HOTKEY arrives: the take/retake
        # event's time is the press, not when the UI thread or the recorder got to it.
        self.ui.post(self.handle, action, foreground, time.perf_counter(), catalog.now_iso())

    def _on_problem_thread(self, action, chord, message, final) -> None:
        self.ui.post(self._hotkey_problem, action, chord, message, final)

    def _on_registered_thread(self, action, chord) -> None:
        self.ui.post(self._publish)

    def _hotkey_problem(self, action, chord, message, final) -> None:
        key = f"{action}_hotkey"
        if final:
            text = f"peep: {message}. Change [agent] {key} in config.toml, then `peep agent reload`."
        else:
            text = f"peep: {message}; retrying for {self.cfg.agent.hotkey_retry_s} s"
        self._toast(text, "error", 12)
        self._publish()

    def handle(self, action: str, foreground: ForegroundInfo | None = None, qpc: float | None = None,
               at: str | None = None) -> None:
        if self.exiting:
            event(log, logging.INFO, "agent.hotkey_ignored", action=action, why="exiting")
            return
        if action == "record":
            self._on_record(foreground)
        elif action == "mark":
            self._on_mark()
        elif action == "discard":
            self._on_discard()
        elif action in EVENT_ACTIONS:
            self._on_event(action, qpc, at)
        else:
            event(log, logging.WARNING, "agent.unknown_action", action=action)

    def _on_event(self, action: str, qpc: float | None, at: str | None) -> None:
        """Pause / take / retake: a request for whichever recording is live (the
        agent's own or a terminal one). The recorder decides it (debounce, take
        state), shows the corner patch and publishes the result for the pill."""
        if self.session is not None and self.session.stop_kind is not None:
            self._toast("still saving the last take…", "info", 2)
            return
        try:
            self.control.request_event(EVENT_ACTIONS[action], "hotkey", qpc=qpc, at=at)
        except LookupError:
            self._toast(f"nothing is recording, so there is nothing to {action}", "info", 2.5)
            return
        except OSError as exc:
            event(log, logging.ERROR, "agent.event_failed", action=action, error=repr(exc))
            self._toast(f"{action} failed: {exc}", "error", 6)
            return
        event(log, logging.INFO, "agent.event_requested", action=action)

    def _on_record(self, foreground) -> None:
        if self.session is not None:
            if self.session.stop_kind is None:
                self._stop_own("stop")
            else:
                self._toast("still saving the last take…", "info", 2)
            return
        foreign = self.control.live_recording()
        if foreign is not None:
            try:
                self.control.request_stop()
                self._toast("stopping the terminal recording (it keeps its terminal behaviour: no dialog)", "info", 4)
            except LookupError:
                self._start(foreground)     # it finished in between
            return
        if self.dialog is not None and not self.dialog.closed:
            self.dialog.resolve(dialog.KEEP)     # a new take while naming the last: keep the automatic name
        self._start(foreground)

    def _on_mark(self) -> None:
        try:
            self.control.request_mark("hotkey")
        except LookupError:
            self._toast("nothing is recording, so there is nothing to mark", "info", 2.5)
            return
        except OSError as exc:
            event(log, logging.ERROR, "agent.mark_failed", error=repr(exc))
            self._toast(f"mark failed: {exc}", "error", 6)
            return
        self._toast("◆ mark", "info", 1.2)

    def _on_discard(self) -> None:
        if self.session is not None:
            if self.session.stop_kind is None:
                self._stop_own("discard")
            elif self.session.stop_kind == "stop":
                self.session.stop_kind = "discard"      # pressed while saving: delete it once saved
                self._toast("will discard once saved…", "warning", 2)
            return
        if self.dialog is not None and not self.dialog.closed:
            self.dialog.resolve(dialog.DISCARD)
            return
        foreign = self.control.live_recording()
        if foreign is not None:
            try:
                self.control.request_stop()
            except LookupError:
                pass
            self._toast("stopping the terminal recording, then discarding it…", "warning", 3)
            self.spawn(self._discard_foreign_when_done, foreign)
            return
        self._toast("nothing is recording, so there is nothing to discard", "info", 2.5)

    # -- recording ------------------------------------------------------------------

    def next_collection(self) -> str:
        last = self.prefs.last_collection()
        if last:
            return last
        try:
            return naming.validate_collection(self.cfg.default_collection)
        except naming.NamingError:
            return "inbox"

    def _start(self, foreground: ForegroundInfo | None) -> None:
        collection = self.next_collection()
        req = RecordRequest(collection=collection, origin="agent",
                            foreground=foreground if foreground is not None else ForegroundInfo(None, None))
        ev = threading.Event()
        ev.reason = None
        self.session = Session(req, collection, ev)
        event(log, logging.INFO, "agent.record_start", collection=collection,
              fg_process=req.foreground.process, fg_title=req.foreground.title)
        self._show_pill("starting", None)
        self.spawn(self._work, self.session, self.cfg)

    def _stop_own(self, kind: str) -> None:
        s = self.session
        s.stop_kind = kind
        s.stop_event.reason = STOP_REASONS[kind]
        s.stop_event.set()
        event(log, logging.INFO, "agent.record_stop", kind=kind)
        self._show_pill("stopping", None)

    def _work(self, session: Session, cfg: config_mod.Config) -> None:
        """Worker thread: A's recorder, unchanged. Whatever happens comes back to the UI thread."""
        result, error = None, None
        try:
            recorder = self.recorder_factory(cfg, self._recorder_status)
            result = recorder.record(session.request, session.stop_event)
        except BaseException as exc:
            error = exc
            if not isinstance(exc, (RecordError, AlreadyRecording, naming.NamingError)):
                log.exception("recording raised")
        self.ui.post(self._finished, session, result, error)

    def _recorder_status(self, line: str) -> None:
        """A's recorder prints status lines for a terminal; the agent logs them and
        surfaces the warnings ('! ...') on screen."""
        event(log, logging.INFO, "agent.recorder_status", line=line)
        if line.startswith("!"):
            self.ui.post(self._toast, line.lstrip("! "), "warning", 6)

    def _finished(self, session: Session, result, error) -> None:
        self.session = None
        kind = session.stop_kind
        if error is not None:
            if isinstance(error, AlreadyRecording):
                self._toast(f"not started: {error}", "warning", 5)
            else:
                self._toast(f"recording could not start: {error}", "error", 10)
            event(log, logging.ERROR, "agent.record_error", error=repr(error), kind=kind)
        elif not result.ok:
            event(log, logging.ERROR, "agent.record_failed", message=result.message, kind=kind)
            if kind == "discard" and result.uid and result.media_path is not None:
                self._discard(result.uid, "hotkey")
            else:
                self._toast(result.message, "error", 12)
        elif kind == "discard":
            self._discard(result.uid, "hotkey")
        elif kind == "exit" or not self.cfg.agent.dialog:
            self.prefs.set_last_collection(session.collection)
            self._toast(f"saved {result.media_path.name}", "info", 4)
        else:
            self._open_dialog(session, result)
        self._show_pill(None, None)
        if self.exiting:
            self._quit()

    # -- the dialog -------------------------------------------------------------------

    def _open_dialog(self, session: Session, result) -> None:
        media: Path = result.media_path
        # The prefill is the name the file actually has, counter included ("...-2"): before
        # C1a it dropped the counter and so offered the name of another recording (notes.md).
        auto_slug = naming.slug_of_stem(catalog.sidecar_path(media).stem)
        try:
            size = media.stat().st_size
        except OSError:
            size = None
        root = self.cfg.root_path()
        try:
            recent = catalog.Catalog(root).recent_collections()
        except OSError as exc:
            event(log, logging.WARNING, "agent.recent_collections_failed", error=repr(exc))
            recent = []
        if getattr(result, "size_bytes", None) is not None:
            size = result.size_bytes
        uid = result.uid
        model = dialog.DialogModel(
            name=auto_slug, collection=session.collection,
            choices=dialog.collection_choices(session.collection, recent, self.cfg.default_collection),
            status=dialog.status_line(result.duration_s, size, str(media),
                                      events.summary_line(getattr(result, "summary", None))),
            preview=lambda name, collection: self.save_preview(uid, name, collection))

        def on_result(action, name, collection, options):
            return self.dialog_result(uid, session.collection, media.name, action, name, collection)

        event(log, logging.INFO, "agent.dialog_open", uid=uid, prefill=auto_slug, collection=session.collection)
        self.dialog = self.ui.open_dialog(model, on_result)

    def dialog_result(self, uid: str, recorded_in: str, file_name: str, action: str, name: str,
                      collection: str) -> str | None:
        """The dialog's three outcomes. None closes the dialog; a string is shown
        in it and the dialog stays (e.g. the file is open in a player)."""
        root = self.cfg.root_path()
        if action == dialog.KEEP:
            self.prefs.set_last_collection(recorded_in)
            self._toast(f"kept {file_name}", "info", 3)
            event(log, logging.INFO, "agent.dialog_keep", uid=uid)
            return None
        if action == dialog.DISCARD:
            return self._discard(uid, "dialog")
        if action == dialog.SAVE:
            choice, problem = dialog.validate_choice(name, collection)
            if problem:
                return problem
            try:
                entry = catalog.rename(root, uid, choice.name, choice.collection, live_uid=self._live_uid())
            except (OSError, LookupError, naming.NamingError, ValueError) as exc:
                event(log, logging.ERROR, "agent.rename_failed", uid=uid, error=repr(exc))
                return f"could not rename: {exc}"
            self.prefs.set_last_collection(choice.collection)
            self._toast(f"saved {entry.file}" + (" (that name was taken, so it got a counter)"
                                                 if getattr(entry, "suffixed", False) else ""), "info", 4)
            event(log, logging.INFO, "agent.dialog_save", uid=uid, file=entry.file,
                  suffixed=getattr(entry, "suffixed", False))
            return None
        event(log, logging.WARNING, "agent.dialog_unknown_action", action=action)
        return None

    def _live_uid(self) -> str | None:
        return (self.control.live_recording() or {}).get("uid")

    def save_preview(self, uid: str, name: str, collection: str) -> str:
        """What Enter would save as, for the dialog's live preview line: the same
        allocation the rename then performs (catalog.plan_rename)."""
        choice, problem = dialog.validate_choice(name, collection)
        if problem:
            return problem
        try:
            plan = catalog.plan_rename(self.cfg.root_path(), uid, choice.name, choice.collection)
        except (OSError, LookupError, naming.NamingError, ValueError) as exc:
            return f"cannot save: {exc}"
        return dialog.preview_text(plan)

    # -- discard ----------------------------------------------------------------------

    def _discard(self, uid: str, reason: str) -> str | None:
        try:
            entry = catalog.discard(self.cfg.root_path(), uid, reason, live_uid=self._live_uid())
        except (OSError, LookupError) as exc:
            event(log, logging.ERROR, "agent.discard_failed", uid=uid, error=repr(exc))
            message = f"could not discard: {exc}"
            self._toast(message, "error", 10)
            return message
        self._toast(f"discarded {Path(entry.file).name or entry.title}", "warning", 4)
        return None

    def _discard_foreign_when_done(self, info: dict) -> None:
        """Worker thread: wait for a terminal recording to finalize, then discard it."""
        pid, uid = int(info.get("pid", 0)), info.get("uid")
        finished = self.control.wait_finished(pid, timeout_s=180)
        if not finished or not uid:
            event(log, logging.ERROR, "agent.foreign_discard_timeout", pid=pid, uid=uid)
            self.ui.post(self._toast, "the terminal recording did not finish; nothing was discarded", "error", 10)
            return
        self.ui.post(self._discard, uid, "hotkey")

    # -- the tick (UI thread, every 250 ms) ------------------------------------------------

    def tick(self) -> None:
        self._ticks += 1
        if not self.exiting:
            command = self.agent_control.take_command()
            if command == "stop":
                self.shutdown("peep agent stop")
                return
            if command == "reload":
                self.reload("peep agent reload")
            if self._ticks % CONFIG_CHECK_EVERY == 0:
                mtime = self.config_mtime()
                if mtime != self._mtime:
                    self._mtime = mtime
                    self.reload("config.toml changed")
        self._refresh_pill()

    def _refresh_pill(self) -> None:
        info = self.control.live_recording()
        if info is not None:
            status = info.get("status") or "recording"
            if self.session is not None and self.session.stop_kind is not None:
                status = "stopping"
            if status not in PILL_STATES:
                status = "recording"
            captured = info.get("captured_s") if isinstance(info.get("captured_s"), (int, float)) else 0.0
            since = _parse_iso(info.get("recording_since"))
            if status == "recording" and since:
                elapsed = captured + (self.now() - since).total_seconds()
            else:
                elapsed = captured
            self._show_pill(status, elapsed, int(info.get("takes") or 0), bool(info.get("take_open")))
        elif self.session is not None:
            self._show_pill("stopping" if self.session.stop_kind else "starting", None)
        else:
            self._show_pill(None, None)

    def _show_pill(self, status: str | None, elapsed: float | None, takes: int = 0, take_open: bool = False) -> None:
        ag = self.cfg.agent
        if status is None or not ag.pill or self.pill_problem:
            self.ui.pill(None)
            return
        if not self.ui.pill(pill_text(status, elapsed, takes, take_open), COLORS[status], ag.pill_position,
                            ag.pill_margin_px):
            self._pill_failed()

    def _pill_failed(self) -> None:
        if self.pill_problem:
            return
        self.pill_problem = "capture exclusion could not be set; pill disabled so it cannot appear in recordings"
        event(log, logging.ERROR, "agent.pill_disabled", why=self.pill_problem)
        self.ui.pill(None)
        self._publish()

    def _toast(self, text: str, level: str = "info", seconds: float = 3) -> None:
        ag = self.cfg.agent
        safe_to_show_unexcluded = self.control.live_recording() is None and self.session is None
        shown = self.ui.toast(text, COLORS.get(level, COLORS["info"]), seconds, ag.pill_position,
                              ag.pill_margin_px, safe_to_show_unexcluded)
        event(log, logging.INFO if shown else logging.WARNING, "agent.toast", kind=level, text=text, shown=shown)

    # -- config reload ----------------------------------------------------------------

    def reload(self, why: str) -> bool:
        try:
            new = self.load_config()
        except config_mod.ConfigError as exc:
            event(log, logging.ERROR, "agent.reload_failed", why=why, error=str(exc))
            self._toast(f"config not reloaded: {exc}", "error", 12)
            self._publish(config_error=str(exc))
            return False
        old_table = hotkeys.table_from_agent_config(self.cfg.agent)
        new_table = hotkeys.table_from_agent_config(new.agent)
        retry_changed = new.agent.hotkey_retry_s != self.cfg.agent.hotkey_retry_s
        self.cfg = new
        self.config_loaded_at = catalog.now_iso()
        if new_table != old_table or retry_changed:
            if self.listener is not None:
                self.listener.stop()
            self._start_listener()
        if new.agent.pill and not self.pill_problem and not self.ui.prepare_overlays():
            self._pill_failed()
        event(log, logging.INFO, "agent.reloaded", why=why, config=new.source,
              hotkeys_changed=new_table != old_table)
        self._toast("config reloaded" + (" (hotkeys re-registered)" if new_table != old_table else ""), "info", 2.5)
        self._publish(config_error=None)
        return True

    # -- status (agent.json) -------------------------------------------------------------

    def _check_install(self) -> None:
        status = self.install_check()
        self.ui.post(self._install_checked, status)

    def _install_checked(self, status: dict) -> None:
        self.install = status
        event(log, logging.INFO if status.get("state") != "stale" else logging.WARNING, "agent.install_status",
              **{k: v for k, v in status.items() if k in ("state", "detail", "changed")})
        if status.get("state") == "stale":
            self._toast("peep agent: the installed copy is older than the repo; run `peep agent restart` in WSL",
                        "warning", 8)
        self._publish()

    def _publish(self, **fields) -> None:
        info = {"version": __version__, "code": self.code, "config": self.cfg.source,
                "config_loaded_at": self.config_loaded_at,
                "hotkeys": self.listener.status() if self.listener else {},
                "pill": ("off" if not self.cfg.agent.pill else f"disabled: {self.pill_problem}" if self.pill_problem
                         else "on"),
                "install": self.install, **fields}
        try:
            self.agent_control.update(**info)
        except OSError as exc:
            event(log, logging.WARNING, "agent.publish_failed", error=repr(exc))


# ---------------------------------------------------------------------------
# 3. run_agent
# ---------------------------------------------------------------------------


def _say(message: str) -> None:
    """stderr when there is one (python.exe from a console); pythonw has none."""
    if sys.stderr is not None:
        try:
            print(f"peep: {message}", file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass


def run_agent(cfg: config_mod.Config) -> int:
    """The resident process. Returns the exit code; never leaves agent.json behind."""
    from . import winapi
    from .recorder import Recorder
    from .ui import TkUi
    state = paths.state_dir()
    agent_control = AgentControl(state, winapi.pid_alive)
    mutex = winapi.SingleInstance()
    if not mutex.acquired:
        info = agent_control.live() or {}
        msg = f"the peep agent is already running (pid {info.get('pid', '?')}); this second one exits"
        event(log, logging.WARNING, "agent.second_instance", running_pid=info.get("pid"))
        _say(msg)
        return 1
    app = lifecycle.app_dir()
    marker = lifecycle.read_marker(app)
    code = lifecycle.code_id(marker["files"]) if marker and marker.get("files") else None
    try:
        agent_control.claim({"state": "starting", "version": __version__, "code": code,
                             "python": sys.executable, "app": str(app)})
    except AgentAlreadyRunning as exc:
        event(log, logging.WARNING, "agent.second_instance", error=str(exc))
        _say(f"{exc}; this second one exits")
        return 1
    try:
        winapi.set_dpi_aware()
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        control = Control(state, winapi.pid_alive)
        ui = TkUi(root)

        def recorder_factory(run_cfg, status):
            return Recorder(run_cfg, control, flasher_factory=ui.flasher_factory, status=status)

        def listener_factory(table, on_hotkey, on_problem, on_registered, retry_s):
            return hotkeys.HotkeyListener(table, on_hotkey, on_problem, on_registered, retry_s=retry_s)

        def config_mtime():
            try:
                return os.stat(paths.config_path()).st_mtime
            except OSError:
                return None

        agent = Agent(cfg, ui=ui, control=control, agent_control=agent_control,
                      prefs=Prefs(paths.data_dir() / PREFS_NAME), recorder_factory=recorder_factory,
                      listener_factory=listener_factory, config_mtime=config_mtime,
                      install_check=lambda: lifecycle.install_status(app), code=code)
        agent.start()
        root.mainloop()
        if not agent.quit_done:              # the Tk loop ended some other way (e.g. a logoff)
            agent.shutdown("tk loop ended")
        return 0
    finally:
        agent_control.release()
