"""The resident agent (`peep agent run`, launched by pythonw.exe at login):
global hotkeys, the REC pill, the stop dialog, marks, and its own lifecycle.

  Ctrl+Alt+R   nothing recording: start (foreground window read at the press,
               last-used collection). Recording: stop, then the naming dialog.
  Ctrl+Alt+M   drop a mark: a cyan flash and an entry in the sidecar's `marks`
  Ctrl+Alt+X   stop and delete the current take (catalogued as discarded)
  Ctrl+Alt+P   hard pause / resume (session C1a): capture stops, the next press opens a new segment
  Ctrl+Alt+T   take: open one, or close the open one (a blue / red corner patch)
  Ctrl+Alt+Backspace
               correct (C1c; was retake): the chart's two-level undo of the current take,
               moving its close here or dropping it and restarting (a yellow corner patch)
               (all three, like mark, act on any live recording through control files;
               the recorder decides them in events.EventModel, debounce included)

  Ctrl+Alt+[   learn the screen now showing as this recording's start screen (C1d)
  Ctrl+Alt+]   learn it as the end screen (C1d); `peep learn start|end` and
               `peep forget start|end` do the same from a terminal

Auto-takes (C1d): a learn press captures the screen (sampler.Sampler, on its
own thread: it waits up to ~1 s for the screen to hold still) and sends it to
the recorder as a `learn` request; the recorder's event model decides what
that appearance does to the takes and publishes the result in active.json's
`auto_takes`. Once the recorder has confirmed a reference, the sampler matches
it a few times a second while the recording captures (never while paused, and
not at all while nothing is learned), and sends each certain appearance or
disappearance as a `screen` request. A screen learned to look like the other
kind's is refused here, before anything is sent but the refusal itself.

The REC pill (C1c) is two lines: the state (time, take, kept time), then
what each key does now, computed by events.next_effect from the take state
the recorder publishes in active.json, the same function the recorder's
event model decides presses with. After each press its feedback replaces the
second line for 1.5 s. `[agent] pill_hints = false` keeps the first line only
(feedback still shows). See pill.pill_lines.

Threads: the Tk thread runs everything in this class (through ui.post /
ui.every, serialised by the UI dispatcher); the hotkey listener thread
only forwards presses; one worker thread per recording runs A's
`Recorder.record()` unchanged, with the agent's marshalled flasher and a
`stop_event` the hotkeys set (A's Proposal 1).

Session C1b: after the dialog's Save or Esc (or straight after the stop when
the dialog is off), the recording's cut is rendered in the background
(render.auto), on the render queue's own worker thread: one render at a time,
in order, a toast when it starts and when it finishes or fails, progress in
agent.json (`peep agent status`). The agent stays responsive throughout and a
new recording may start while a render runs. A failed render leaves the
original untouched; `peep render` retries it.

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
  1. Prefs (last-used collection)   (~line 70)
  2. Agent                          (~line 145)
  3. run_agent                      (~line 950)
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

from . import __version__, catalog, config as config_mod, dialog, events, hotkeys, lifecycle, naming, paths, screens
from . import render as render_mod
from .catalog import write_json_atomic
from .control import AgentAlreadyRunning, AgentControl, AlreadyRecording, Control
from .logsetup import event
from . import pill as pill_mod
from .pill import COLORS
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
EVENT_ACTIONS = {"pause": "pause-toggle", "take": "take", "correct": "correct",
                 "retake": "correct"}        # C1c renamed retake; the old action name still works
PILL_STATES = ("starting", "recording", "pausing", "paused", "resuming", "stopping")
LEARN_ACTIONS = {"learn_start": "start", "learn_end": "end"}      # C1d: hotkey action -> screen kind
LEARN_CONFIRM_S = 5.0          # a learn the recorder has not confirmed by then is given up (logged)


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
                 spawn: Callable = _thread, now: Callable[[], _dt.datetime] | None = None,
                 render_queue=None, sampler=None):
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
        self.render_queue = render_queue          # render.RenderQueue (run_agent); None: no background renders
        self.render_state: dict | None = None     # the render in progress, for agent.json
        self._render_hints: dict = {}             # uid -> {"summary", "segments"}, from the recording's result
        self._feedback_key = None                 # (uid, seq) of the last press feedback seen in active.json
        self._feedback: dict | None = None        # ...and the feedback itself, shown until _feedback_until
        self._feedback_until: _dt.datetime | None = None
        self._mtime = self.config_mtime()
        self.config_loaded_at = catalog.now_iso()
        # C1d, auto-takes: the live sampler (None: this agent cannot sample, said once at start), the
        # recording its references belong to, learns sent but not yet confirmed by the recorder, and
        # references whose PNG could not be read back (not retried every tick).
        self.sampler = sampler
        self._auto_uid: str | None = None
        self._auto_pending: dict = {}
        self._auto_counter = 0
        self._auto_unreadable: set = set()
        self._sampler_published: dict | None = None
        self._auto_mismatch_said: set = set()

    # -- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        if self.sampler is not None:
            self._configure_sampler()
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
        if self.render_queue is not None:
            dropped = self.render_queue.stop()
            if dropped or self.render_state:
                event(log, logging.WARNING, "agent.renders_stopped", running=(self.render_state or {}).get("name"),
                      dropped=[j["name"] for j in dropped], hint="`peep render` renders them")
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
        if self.sampler is not None:
            self.sampler.stop()
        self.agent_control.release()
        event(log, logging.INFO, "agent.exit")
        self.ui.quit()

    # -- hotkeys (listener thread -> UI thread) ---------------------------------------

    def on_hotkey(self, action: str, foreground: ForegroundInfo | None) -> None:
        # Stamped here, on the listener thread the instant WM_HOTKEY arrives: the take/correct
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
        elif action in LEARN_ACTIONS:
            self.learn(LEARN_ACTIONS[action], "hotkey", qpc, at)
        else:
            event(log, logging.WARNING, "agent.unknown_action", action=action)

    def _on_event(self, action: str, qpc: float | None, at: str | None) -> None:
        """Pause / take / correct: a request for whichever recording is live (the
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

    # -- auto-takes (C1d) -------------------------------------------------------------

    def _configure_sampler(self) -> None:
        at = self.cfg.auto_takes
        self.sampler.configure(config_mod.screen_thresholds(at), at.sample_hz, at.learn_wait_ms / 1000,
                               width=at.thumb_width)

    def learn(self, kind: str, source: str, qpc: float | None = None, at: str | None = None) -> None:
        """A learn press (hotkey or `peep learn`): capture the screen now, then send it.
        Every way this cannot happen is said on screen and logged."""
        qpc = qpc if qpc is not None else time.perf_counter()
        at = at or catalog.now_iso()
        if not self.cfg.auto_takes.enabled:
            self._toast("auto-takes are off ([auto_takes] enabled = false)", "info", 3)
            return
        if self.sampler is None:
            self._toast("auto-takes cannot read the screen in this agent (see agent.log)", "error", 6)
            return
        info = self.control.live_recording()
        if info is None:
            self._toast(f"nothing is recording, so there is no {kind} screen to learn (it belongs to one recording)",
                        "info", 3)
            return
        status = info.get("status") or "recording"
        if status in ("paused", "pausing", "resuming"):
            self._send_learn(kind, source, qpc, at, {"refused": "paused"})       # recorded, and the pill says why
            return
        if status != "recording" or (self.session is not None and self.session.stop_kind is not None):
            self._toast(f"the recording is {status}; learn the {kind} screen once it is recording", "info", 3)
            return
        uid = info.get("uid")
        event(log, logging.INFO, "agent.learn_capture", screen=kind, source=source)
        self.sampler.capture(lambda thumb, size, stable, waited, error: self.ui.post(
            self._learn_captured, kind, source, qpc, at, uid, thumb, size, stable, waited, error))

    def _learn_captured(self, kind, source, qpc, at, uid, thumb, size, stable, waited, error) -> None:
        if error is not None or thumb is None:
            event(log, logging.ERROR, "agent.learn_failed", screen=kind, error=error)
            self._toast(f"could not read the screen to learn it: {error}", "error", 6)
            return
        info = self.control.live_recording()
        if info is None or info.get("uid") != uid:
            self._toast(f"the recording ended before the {kind} screen was learned", "info", 3)
            return
        thr = config_mod.screen_thresholds(self.cfg.auto_takes)
        other = screens.OTHER[kind]
        active = self.sampler.active()
        other_thumb = (self._auto_pending.get(other) or active.get(other) or {}).get("thumb")
        if other_thumb is not None and screens.same_screen(thumb, other_thumb, thr):
            self._send_learn(kind, source, qpc, at, {"refused": f"same-as-{other}-screen"})
            return
        cur = active.get(kind)
        same = bool(cur and cur["present"] and screens.same_screen(thumb, cur["thumb"], thr))
        self._auto_counter += 1
        ref = f"{kind}-{os.getpid()}-{self._auto_counter}"     # unique across agent restarts (review finding)
        extra = {"ref": ref, "thumb": screens.to_hex(thumb), "size": list(size), "stable": bool(stable),
                 "waited_s": waited, "thresholds": thr.to_dict(), "same_as_current": same,
                 "sampler": {"hz": self.cfg.auto_takes.sample_hz, "thumb_width": self.cfg.auto_takes.thumb_width}}
        if self._send_learn(kind, source, qpc, at, extra):
            self._auto_recording(uid)             # a learn right at the start may beat the first tick's sync
            self._auto_pending[kind] = {"ref": ref, "thumb": thumb, "thr": thr, "size": tuple(size),
                                        "since": time.monotonic()}

    def _send_learn(self, kind: str, source: str, qpc: float, at: str, extra: dict) -> bool:
        try:
            self.control.request_event("learn", source, qpc=qpc, at=at, extra={"screen": kind, **extra})
        except LookupError:
            self._toast(f"the recording ended before the {kind} screen was learned", "info", 3)
            return False
        except OSError as exc:
            event(log, logging.ERROR, "agent.learn_send_failed", screen=kind, error=repr(exc))
            self._toast(f"learning the {kind} screen failed: {exc}", "error", 6)
            return False
        event(log, logging.INFO, "agent.learn_sent", screen=kind, source=source, ref=extra.get("ref"),
              refused=extra.get("refused"), stable=extra.get("stable"), same_as_current=extra.get("same_as_current"))
        return True

    def forget(self, kind: str, source: str = "cli") -> None:
        """`peep forget start|end`: the screen stops counting from now on."""
        try:
            self.control.request_event("forget", source, extra={"screen": kind})
        except LookupError:
            self._toast(f"nothing is recording, so there is no {kind} screen to forget", "info", 3)
            return
        except OSError as exc:
            event(log, logging.ERROR, "agent.forget_failed", screen=kind, error=repr(exc))
            self._toast(f"forgetting the {kind} screen failed: {exc}", "error", 6)
            return
        self._auto_pending.pop(kind, None)
        if self.sampler is not None:
            self.sampler.drop(kind)
        event(log, logging.INFO, "agent.forget_sent", screen=kind, source=source)

    def _emit_screen(self, kind: str, ref: str, change: dict) -> None:
        """Sampler thread: a learned screen appeared or went away (certain, by its
        hysteresis), stamped with its first sample. LookupError (the recording just
        ended) is the sampler's to log."""
        self.control.request_event("screen", "visual", qpc=change["qpc"],
                                   extra={"screen": kind, "ref": ref, "change": change["change"],
                                          "score": change.get("score"), "samples": change.get("samples")})

    def _sync_auto(self, info: dict | None) -> None:
        """Every tick: the sampler follows what the recorder published. A new
        recording clears it; a reference the recorder confirmed is matched from
        now on (present: it was learned while showing); a forgotten one stops; a
        reference this agent does not hold (it was restarted mid-recording) is
        read back from its PNG. Sampling runs only while capturing."""
        if self.sampler is None:
            return
        self._auto_recording((info or {}).get("uid"))
        if info is None:
            self.sampler.set_running(False)
            return
        block = info.get("auto_takes") if isinstance(info.get("auto_takes"), dict) else {}
        active = self.sampler.active()
        for kind in screens.KINDS:
            pub = block.get(kind) if isinstance(block.get(kind), dict) else {}
            ref = pub.get("ref") if pub.get("learned") else None
            pend = self._auto_pending.get(kind)
            if pend is not None:
                last = pub.get("last_learn") if isinstance(pub.get("last_learn"), dict) else {}
                if ref == pend["ref"]:
                    self.sampler.activate(kind, ref, pend["thumb"], present=True, thresholds=pend["thr"],
                                          size=pend["size"])
                    self._auto_pending.pop(kind)
                    continue
                if last.get("ref") == pend["ref"] and last.get("outcome") == "refused":
                    event(log, logging.INFO, "agent.learn_refused", screen=kind, ref=pend["ref"],
                          reason=last.get("reason"))
                    self._auto_pending.pop(kind)
                elif time.monotonic() - pend["since"] > LEARN_CONFIRM_S:
                    event(log, logging.WARNING, "agent.learn_unconfirmed", screen=kind, ref=pend["ref"])
                    self._auto_pending.pop(kind)
                else:
                    continue                          # not decided yet: leave the sampler as it is
            if ref is None:
                if kind in active:
                    self.sampler.drop(kind)
            elif kind not in active or active[kind]["ref"] != ref:
                self._adopt_published(kind, ref, pub)
        self.sampler.set_running(info.get("status") == "recording" and self.cfg.auto_takes.enabled)

    def _auto_recording(self, uid: str | None) -> None:
        """Learned screens belong to one recording: another one (or none) clears them."""
        if uid != self._auto_uid:
            self.sampler.clear()
            self._auto_pending.clear()
            self._auto_unreadable.clear()
            self._auto_mismatch_said.clear()
            self._auto_uid = uid

    def _adopt_published(self, kind: str, ref: str, pub: dict) -> None:
        """A reference the recorder holds and this agent does not (restarted mid-recording):
        read back from the PNG the recorder wrote. Unreadable: said once, not retried."""
        if ref in self._auto_unreadable:
            return
        try:
            data = Path(str(pub.get("thumb") or "")).read_bytes()
            w, h, thumb = screens.read_png_grey(data)
        except (OSError, ValueError) as exc:
            self._auto_unreadable.add(ref)
            event(log, logging.ERROR, "agent.reference_unreadable", screen=kind, ref=ref, path=pub.get("thumb"),
                  error=repr(exc))
            self._toast(f"the learned {kind} screen could not be read back; learn it again", "warning", 6)
            return
        thr = screens.Thresholds.from_dict(pub.get("thresholds"))
        self.sampler.activate(kind, ref, thumb, present=bool(pub.get("present")), thresholds=thr, size=(w, h))
        event(log, logging.INFO, "agent.reference_adopted", screen=kind, ref=ref, size=f"{w}x{h}")

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
            self._render_hints[result.uid] = self._hint(result)
            self.maybe_render(result.uid, result.media_path.name)
        else:
            self._render_hints[result.uid] = self._hint(result)
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
            self.maybe_render(uid, file_name)
            return None
        if action == dialog.DISCARD:
            self._render_hints.pop(uid, None)
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
            self.maybe_render(uid, Path(entry.file).name)
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

    # -- the render (session C1b) ----------------------------------------------------

    @staticmethod
    def _hint(result) -> dict:
        return {"summary": getattr(result, "summary", None), "segments": getattr(result, "segments", 1) or 1}

    def maybe_render(self, uid: str, name: str) -> bool:
        """Queue the recording's cut when render.auto says so. Returns whether it was queued;
        every reason it was not is logged."""
        hint = self._render_hints.pop(uid, None) or {}
        auto = self.cfg.render.auto
        if not render_mod.should_render(auto, hint.get("summary"), hint.get("segments", 1)):
            event(log, logging.INFO, "agent.render_skipped", uid=uid, why=f"render.auto={auto}")
            return False
        if self.exiting:
            event(log, logging.INFO, "agent.render_skipped", uid=uid, why="the agent is exiting; `peep render` does it")
            return False
        if self.render_queue is None:
            event(log, logging.WARNING, "agent.render_skipped", uid=uid, why="no render queue in this agent")
            return False
        try:
            ahead = self.render_queue.submit(uid, name)
        except RuntimeError as exc:
            event(log, logging.WARNING, "agent.render_skipped", uid=uid, why=str(exc))
            return False
        if ahead:
            self._toast(f"✂ {name}: render queued ({ahead} ahead)", "info", 3)
        return True

    def on_render_event(self, kind: str, info: dict) -> None:
        """UI thread: what the render queue's worker reported."""
        name = info.get("name") or "?"
        if kind == "start":
            self.render_state = {"name": name, "uid": info.get("uid"), "stage": "starting", "progress": None,
                                 "started_at": catalog.now_iso()}
            self._toast(f"✂ rendering {name}…", "info", 2.5)
            self._publish(render=self.render_state)
        elif kind == "progress":
            st = self.render_state or {"name": name, "uid": info.get("uid")}
            frac = info.get("fraction")
            last = st.get("progress")
            st["stage"] = info.get("stage")
            if frac is None or last is None or frac >= last + 0.1 or frac >= 1.0:
                st["progress"] = None if frac is None else round(frac, 2)
                self.render_state = st
                self._publish(render=st)
        elif kind == "done":
            res = info.get("result")
            self.render_state = None
            self._publish(render=None)
            if res is not None and not res.up_to_date:
                fb = f" · {len(res.fallbacks)} fallback(s), see `peep render --dry-run`" if res.fallbacks else ""
                self._toast(f"✂ cut ready: {res.cut_path.name} ({events.format_mmss(res.duration_s)} of "
                            f"{events.format_mmss(res.source_s)}){fb}", "warning" if res.fallbacks else "info", 6)
        elif kind == "failed":
            self.render_state = None
            self._publish(render=None)
            self._toast(f"✂ render failed for {name}: {info.get('error')}. The original is kept; "
                        f"`peep render` retries.", "error", 12)
        event(log, logging.INFO if kind != "failed" else logging.ERROR, "agent.render_event", kind=kind, file=name,
              **({"error": info.get("error")} if kind == "failed" else {}))

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
            if command and command.split()[0] in ("learn", "forget"):        # C1d: `peep learn|forget KIND`
                verb, kind = command.split()
                if verb == "learn":
                    self.learn(kind, "cli")
                else:
                    self.forget(kind, "cli")
            if self._ticks % CONFIG_CHECK_EVERY == 0:
                mtime = self.config_mtime()
                if mtime != self._mtime:
                    self._mtime = mtime
                    self.reload("config.toml changed")
                if self.sampler is not None:
                    stats = self.sampler.stats()
                    if stats != self._sampler_published:
                        self._sampler_published = stats
                        self._publish(sampler=stats)
                    for kind in stats.get("mismatched") or []:
                        if kind not in self._auto_mismatch_said:
                            self._auto_mismatch_said.add(kind)
                            self._toast(f"the screen's size changed since the {kind} screen was learned; "
                                        f"learn it again", "warning", 8)
        self._refresh_pill()

    def _refresh_pill(self) -> None:
        info = self.control.live_recording()
        self._sync_auto(info)
        if info is not None:
            status = info.get("status") or "recording"
            if self.session is not None and self.session.stop_kind is not None:
                status = "stopping"
            if status not in PILL_STATES:
                status = "recording"
            captured = info.get("captured_s") if isinstance(info.get("captured_s"), (int, float)) else 0.0
            now = self.now()
            since = _parse_iso(info.get("recording_since"))
            if status == "recording" and since:
                elapsed = captured + (now - since).total_seconds()
            else:
                elapsed = captured
            state = info.get("take_state") if isinstance(info.get("take_state"), dict) else None
            if state is not None:          # an open take keeps growing between presses (C1c)
                at = _parse_iso(state.get("at"))
                state = pill_mod.live_take_state(state, (now - at).total_seconds() if at else None,
                                                 recording=status == "recording")
            self._show_pill(status, elapsed, int(info.get("takes") or 0), bool(info.get("take_open")),
                            state=state, feedback=self._fresh_feedback(info, now))
        elif self.session is not None:
            self._show_pill("stopping" if self.session.stop_kind else "starting", None)
        else:
            self._show_pill(None, None)

    def _fresh_feedback(self, info: dict, now: _dt.datetime) -> dict | None:
        """The last press's feedback from active.json, for FEEDBACK_S after this
        agent first saw it (each press has its own seq, so each shows once). One
        already older than that when first seen (an agent started mid-recording
        finds the last press's) is not shown at all."""
        fb = info.get("feedback")
        if isinstance(fb, dict) and fb.get("seq") is not None:
            key = (info.get("uid"), info.get("pid"), fb.get("seq"))
            if key != self._feedback_key:
                at = _parse_iso(fb.get("at"))
                age = (now - at).total_seconds() if at else 0.0
                stale = age > pill_mod.FEEDBACK_S + pill_mod.FEEDBACK_LATE_S
                self._feedback_key, self._feedback = key, (None if stale else fb)
                self._feedback_until = now + _dt.timedelta(seconds=pill_mod.FEEDBACK_S)
                event(log, logging.INFO, "agent.pill_feedback", kind=fb.get("kind"), action=fb.get("action"),
                      ignored=fb.get("ignored"), seq=fb.get("seq"), age_s=round(age, 2), shown=not stale)
        if self._feedback is not None and self._feedback_until is not None and now < self._feedback_until:
            return self._feedback
        return None

    def _show_pill(self, status: str | None, elapsed: float | None, takes: int = 0, take_open: bool = False,
                   state: dict | None = None, feedback: dict | None = None) -> None:
        ag = self.cfg.agent
        if status is None or not ag.pill or self.pill_problem:
            self.ui.pill(None)
            return
        text = pill_mod.pill_lines(status, elapsed, state, pill_mod.key_labels(ag), hints=ag.pill_hints,
                                   feedback=feedback, takes=takes, take_open=take_open)
        if not self.ui.pill(text, COLORS[status], ag.pill_position, ag.pill_margin_px):
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
        if self.sampler is not None:
            self._configure_sampler()
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
        renders: dict = {}
        holder: dict = {}

        def render_job(uid, progress):
            r = render_mod.Renderer(holder["agent"].cfg, job_factory=winapi.KillOnCloseJob)
            renders["current"] = r
            try:
                return r.render(uid, progress=progress)
            finally:
                renders.pop("current", None)

        def cancel_render():
            r = renders.get("current")
            if r is not None:
                r.cancel()

        queue = render_mod.RenderQueue(render_job, lambda kind, info: ui.post(holder["agent"].on_render_event,
                                                                              kind, info), cancel=cancel_render)

        def recorder_factory(run_cfg, status):
            return Recorder(run_cfg, control, flasher_factory=ui.flasher_factory, status=status)

        sampler = None
        if sys.platform == "win32":
            from .sampler import GdiGrabber, Sampler
            if cfg.video.output_idx != 0:
                event(log, logging.WARNING, "agent.sampler_primary_only", output_idx=cfg.video.output_idx,
                      why="the sampler reads the primary screen; ddagrab records another output")
            sampler = Sampler(GdiGrabber, lambda kind, ref, change: holder["agent"]._emit_screen(kind, ref, change),
                              width=cfg.auto_takes.thumb_width)
        else:
            event(log, logging.WARNING, "agent.no_sampler", why="screen sampling needs Windows (GDI)")

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
                      install_check=lambda: lifecycle.install_status(app), code=code, render_queue=queue,
                      sampler=sampler)
        holder["agent"] = agent
        agent.start()
        root.mainloop()
        if not agent.quit_done:              # the Tk loop ended some other way (e.g. a logoff)
            agent.shutdown("tk loop ended")
        return 0
    finally:
        agent_control.release()
