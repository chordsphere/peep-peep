"""The resident agent's behaviour, driven through its real `Agent` class with
a fake UI, a fake hotkey listener and a fake recorder (real threads, real
control files, real catalog and renames on a temp storage root). Also:
last-used-collection persistence, the agent's control-file protocol
(agent.json / agent-command), and single-instance refusal."""

import datetime as dt
import json
import os
import queue
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import TempDirMixin
from tests.test_catalog import record as catalog_record
from peep import agent as agent_mod
from peep import catalog, config as c, dialog, naming
from peep.agent import Agent, Prefs
from peep.control import AgentAlreadyRunning, AgentControl, AlreadyRecording, Control
from peep.recorder import RecordResult
from peep.winapi import ForegroundInfo

CHROME = ForegroundInfo("Pull requests · peep-peep - Google Chrome",
                        r"C:\Program Files\Google\Chrome\Application\chrome.exe")
ME = os.getpid()


class FakeDialog:
    def __init__(self, model, on_result):
        self.model, self.on_result, self.closed, self.problems = model, on_result, False, []

    def resolve(self, action, name=None, collection=None):
        problem = self.on_result(action, self.model.name if name is None else name,
                                 self.model.collection if collection is None else collection, {})
        if problem:
            self.problems.append(problem)
        else:
            self.closed = True
        return problem


class FakeUi:
    def __init__(self):
        self.q = queue.Queue()
        self.pills, self.toasts, self.dialogs = [], [], []
        self.quit_called = False
        self.pill_ok = True
        self.tick = None

    def post(self, fn, *args):
        self.q.put((fn, args))

    def every(self, ms, fn):
        self.tick = fn

    def prepare_overlays(self):
        return self.pill_ok

    def pill(self, text, color=None, position=None, margin=None):
        self.pills.append(text)
        return text is None or self.pill_ok

    def toast(self, text, color, seconds, position, margin, unexcluded_ok):
        self.toasts.append(text)
        return True

    def open_dialog(self, model, on_result):
        d = FakeDialog(model, on_result)
        self.dialogs.append(d)
        return d

    def quit(self):
        self.quit_called = True

    def pump(self, until, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if until():
                return True
            try:
                fn, args = self.q.get(timeout=0.02)
            except queue.Empty:
                continue
            fn(*args)
        return until()


class FakeListener:
    instances = []

    def __init__(self, table, on_hotkey, on_problem, on_registered, retry_s):
        self.table, self.on_problem, self.retry_s = table, on_problem, retry_s
        self.started = self.stopped = False
        FakeListener.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def status(self):
        return {a: f"{ch.text} registered" for a, ch in self.table.items()}


class AgentHarness(TempDirMixin):
    def setUp(self):
        super().setUp()
        FakeListener.instances = []
        self.root = self.tmp / "Videos" / "peep"
        self.state = self.tmp / "state"
        self.cfg = c.from_mapping({"root": str(self.root)})
        self.control = Control(self.state, pid_alive=lambda pid: pid == ME)
        self.actl = AgentControl(self.state, pid_alive=lambda pid: pid == ME)
        self.actl.claim({"state": "starting"})
        self.prefs = Prefs(self.tmp / "agent-prefs.json")
        self.ui = FakeUi()
        self.requests, self.record_error, self.status_lines = [], None, []
        self.result_ok = True

    def make_agent(self, cfg=None, **kw):
        h = self

        class FakeRecorder:
            def __init__(self, cfg, status):
                self.cfg, self.status = cfg, status

            def record(self, req, ev):
                h.requests.append(req)
                if h.record_error is not None:
                    raise h.record_error
                uid = catalog.new_uid()
                h.control.claim({"uid": uid, "origin": req.origin, "status": "starting", "final": "f"})
                h.control.update_active(status="recording", recording_since=catalog.now_iso())
                try:
                    self.status("● recording → somewhere")
                    self.status("! qsv pipeline failed to start (ffmpeg exit 1); trying qsv-download")
                    ev.wait(10)
                    slug = naming.suggest_slug(req.foreground.image, req.foreground.title)
                    coll_dir = Path(self.cfg.root) / req.collection
                    coll_dir.mkdir(parents=True, exist_ok=True)
                    stem = naming.allocate_stem(str(coll_dir), dt.date(2026, 10, 5), slug)
                    media = coll_dir / f"{stem}.mp4"
                    media.write_bytes(b"x" * 4096)
                    sc = catalog.new_sidecar(uid=uid, slug=slug, collection=req.collection, file=media.name,
                                             created=catalog.now_iso())
                    sc["status"] = "ok" if h.result_ok else "failed"
                    sc["timeline"]["stop_reason"] = getattr(ev, "reason", None)
                    catalog.write_sidecar(catalog.sidecar_path(media), sc)
                    catalog.Catalog(Path(self.cfg.root)).append(
                        {"event": "recorded" if h.result_ok else "failed", "uid": uid,
                         "collection": req.collection, "file": f"{req.collection}/{media.name}",
                         "title": slug, "created": sc["created"], "duration_s": 12.0})
                    h.stop_reason = getattr(ev, "reason", None)
                    msg = f"saved {media} (12.0s)" if h.result_ok else f"recording failed: ffmpeg exited 1; partial capture kept at {media}"
                    return RecordResult(h.result_ok, msg, uid=uid, media_path=media,
                                        sidecar_path=catalog.sidecar_path(media), exit_code=0 if h.result_ok else 1,
                                        duration_s=12.0)
                finally:
                    h.control.release()

        self.agent = Agent(cfg or self.cfg, ui=self.ui, control=self.control, agent_control=self.actl,
                           prefs=self.prefs, recorder_factory=FakeRecorder, listener_factory=FakeListener, **kw)
        self.agent.start()
        return self.agent

    # helpers ------------------------------------------------------------------------

    def wait_live(self):
        self.assertTrue(self.ui.pump(lambda: (self.control.read_active() or {}).get("status") == "recording"))

    def record_and_stop(self, fg=CHROME):
        a = self.agent
        a.handle("record", fg)
        self.wait_live()
        a.handle("record", None)
        self.assertTrue(self.ui.pump(lambda: a.session is None))

    def entries(self):
        return catalog.Catalog(self.root).entries()


class RecordStopDialogTest(AgentHarness, unittest.TestCase):
    def test_hotkey_start_stop_then_save_from_the_dialog(self):
        self.make_agent()
        self.record_and_stop()
        req = self.requests[0]
        self.assertEqual((req.collection, req.origin, req.foreground), ("inbox", "agent", CHROME))
        self.assertEqual(self.stop_reason, "hotkey")
        self.assertEqual(len(self.ui.dialogs), 1)
        dlg = self.ui.dialogs[0]
        self.assertEqual(dlg.model.name, "chrome-pull-requests-peep-peep")     # slug from the window at start
        self.assertEqual(dlg.model.collection, "inbox")
        self.assertEqual(dlg.model.choices[0], "inbox")
        self.assertIn("2026-10-05-chrome-pull-requests-peep-peep.mp4", dlg.model.status)
        self.assertIn("00:12", dlg.model.status)
        self.assertIsNone(dlg.resolve(dialog.SAVE, "Bale: pack demo", "Bale"))
        self.assertTrue(dlg.closed)
        e = self.entries()[-1]
        self.assertEqual((e.file, e.title), ("bale/2026-10-05-bale-pack-demo.mp4", "Bale: pack demo"))
        self.assertTrue((self.root / "bale" / "2026-10-05-bale-pack-demo.json").exists())
        self.assertFalse((self.root / "inbox" / "2026-10-05-chrome-pull-requests-peep-peep.mp4").exists())
        self.assertEqual(self.prefs.last_collection(), "bale")
        self.assertTrue(any("saved bale/2026-10-05-bale-pack-demo.mp4" in t for t in self.ui.toasts))
        # the next hotkey recording starts in the last-used collection
        self.record_and_stop()
        self.assertEqual(self.requests[1].collection, "bale")
        self.assertEqual(self.ui.dialogs[1].model.choices[0], "bale")
        self.assertIn("inbox", self.ui.dialogs[1].model.choices)

    def test_escape_keeps_the_automatic_name(self):
        self.make_agent()
        self.record_and_stop()
        self.assertIsNone(self.ui.dialogs[0].resolve(dialog.KEEP))
        self.assertEqual(self.entries()[-1].file, "inbox/2026-10-05-chrome-pull-requests-peep-peep.mp4")
        self.assertEqual(self.prefs.last_collection(), "inbox")

    def test_save_with_an_invalid_name_keeps_the_dialog_open(self):
        self.make_agent()
        self.record_and_stop()
        dlg = self.ui.dialogs[0]
        dlg.resolve(dialog.SAVE, "!!!", "inbox")
        self.assertFalse(dlg.closed)
        self.assertIn("name:", dlg.problems[0])

    def test_rename_failure_is_shown_in_the_dialog(self):
        self.make_agent()
        self.record_and_stop()
        dlg = self.ui.dialogs[0]
        with mock.patch.object(catalog, "rename", side_effect=PermissionError("[WinError 32] in use by mpv")):
            dlg.resolve(dialog.SAVE, "demo", "inbox")
        self.assertFalse(dlg.closed)
        self.assertIn("WinError 32", dlg.problems[0])
        self.assertIsNone(dlg.resolve(dialog.SAVE, "demo", "inbox"))      # player closed: retry works

    def test_dialog_delete_discards(self):
        self.make_agent()
        self.record_and_stop()
        self.assertIsNone(self.ui.dialogs[0].resolve(dialog.DISCARD))
        self.assertEqual(self.entries(), [])
        self.assertFalse(list((self.root / "inbox").iterdir()))
        last = catalog.Catalog(self.root).events()[-1]
        self.assertEqual((last["event"], last["reason"]), ("discarded", "dialog"))

    def test_record_hotkey_while_naming_keeps_that_take_and_starts_another(self):
        a = self.make_agent()
        self.record_and_stop()
        a.handle("record", CHROME)
        self.assertTrue(self.ui.dialogs[0].closed)
        self.assertEqual(self.prefs.last_collection(), "inbox")
        self.wait_live()
        a.handle("record", None)
        self.assertTrue(self.ui.pump(lambda: len(self.ui.dialogs) == 2))
        self.assertEqual([e.file for e in self.entries()],
                         ["inbox/2026-10-05-chrome-pull-requests-peep-peep.mp4",
                          "inbox/2026-10-05-chrome-pull-requests-peep-peep-2.mp4"])

    def test_dialog_can_be_turned_off(self):
        cfg = c.from_mapping({"root": str(self.root), "agent": {"dialog": False}})
        self.make_agent(cfg)
        self.record_and_stop()
        self.assertEqual(self.ui.dialogs, [])
        self.assertTrue(any(t.startswith("saved 2026-10-05-chrome") for t in self.ui.toasts))
        self.assertEqual(self.prefs.last_collection(), "inbox")

    def test_recorder_warnings_reach_the_screen(self):
        self.make_agent()
        self.record_and_stop()
        self.assertTrue(any(t.startswith("qsv pipeline failed") for t in self.ui.toasts))
        self.assertFalse(any("recording →" in t for t in self.ui.toasts))

    def test_start_refused_is_reported(self):
        self.make_agent()
        self.record_error = AlreadyRecording("already recording (pid 7): C:/v/x.recording.mkv")
        self.agent.handle("record", CHROME)
        self.assertTrue(self.ui.pump(lambda: self.agent.session is None))
        self.assertTrue(any("not started: already recording" in t for t in self.ui.toasts))

    def test_failed_recording_is_reported_not_named(self):
        self.make_agent()
        self.result_ok = False
        self.record_and_stop()
        self.assertEqual(self.ui.dialogs, [])
        self.assertTrue(any("recording failed" in t for t in self.ui.toasts))

    def test_pill_follows_the_recording(self):
        a = self.make_agent()
        a.handle("record", CHROME)
        self.assertEqual(self.ui.pills[-1], "● starting…")
        self.wait_live()
        a.tick()
        self.assertTrue(self.ui.pills[-1].startswith("● 00:0"), self.ui.pills[-1])
        a.handle("record", None)
        self.assertEqual(self.ui.pills[-1], "■ saving…")
        self.ui.pump(lambda: a.session is None)
        a.tick()
        self.assertIsNone(self.ui.pills[-1])


class DiscardMarkForeignTest(AgentHarness, unittest.TestCase):
    def test_discard_hotkey_deletes_the_take_without_a_dialog(self):
        a = self.make_agent()
        a.handle("record", CHROME)
        self.wait_live()
        a.handle("discard", None)
        self.assertTrue(self.ui.pump(lambda: a.session is None))
        self.assertEqual(self.stop_reason, "hotkey-discard")
        self.assertEqual(self.ui.dialogs, [])
        self.assertEqual(self.entries(), [])
        self.assertFalse(list((self.root / "inbox").iterdir()))
        self.assertTrue(any(t.startswith("discarded") for t in self.ui.toasts))

    def test_discard_hotkey_while_naming_discards_that_take(self):
        a = self.make_agent()
        self.record_and_stop()
        a.handle("discard", None)
        self.assertTrue(self.ui.dialogs[0].closed)
        self.assertEqual(self.entries(), [])

    def test_nothing_to_discard_or_mark_says_so(self):
        a = self.make_agent()
        a.handle("discard", None)
        a.handle("mark", None)
        self.assertEqual(self.ui.toasts, ["nothing is recording, so there is nothing to discard",
                                          "nothing is recording, so there is nothing to mark"])

    def test_mark_hotkey_writes_a_mark_request(self):
        a = self.make_agent()
        a.handle("record", CHROME)
        self.wait_live()
        a.handle("mark", None)
        reqs = list(self.state.glob("mark-*.json"))
        self.assertEqual(len(reqs), 1)
        self.assertEqual(json.loads(reqs[0].read_text())["source"], "hotkey")
        self.assertIn("◆ mark", self.ui.toasts)
        a.handle("record", None)
        self.ui.pump(lambda: a.session is None)

    def test_terminal_recording_record_hotkey_requests_stop_no_dialog(self):
        a = self.make_agent()
        self.control.claim({"uid": "u", "origin": "terminal", "status": "recording", "final": "C:/v/x.mp4"})
        a.handle("record", CHROME)
        self.assertIsNone(a.session)
        self.assertTrue((self.state / "stop-request").exists())
        self.assertTrue(any("no dialog" in t for t in self.ui.toasts))
        a.handle("mark", None)                        # marks work on terminal recordings too
        self.assertEqual(len(list(self.state.glob("mark-*.json"))), 1)

    def test_terminal_recording_discard_hotkey_deletes_after_it_finishes(self):
        a = self.make_agent()
        uid = catalog_record(self.root, "inbox", "2026-10-05-terminal-peep-peep")
        self.control.claim({"uid": uid, "origin": "terminal", "status": "recording"})
        a.handle("discard", None)
        self.assertTrue((self.state / "stop-request").exists())
        self.control.release()                        # the terminal recorder finished
        self.assertTrue(self.ui.pump(lambda: not self.entries()))
        self.assertFalse((self.root / "inbox" / "2026-10-05-terminal-peep-peep.mp4").exists())


class LifecycleTest(AgentHarness, unittest.TestCase):
    def test_hotkey_problem_names_the_chord_and_the_fix(self):
        a = self.make_agent()
        chord = FakeListener.instances[0].table["record"]
        a._on_problem_thread("record", chord, "Ctrl+Alt+R is already taken by another app (winerror 1409)", False)
        a._on_problem_thread("record", chord, "Ctrl+Alt+R is already taken by another app (winerror 1409)", True)
        self.ui.pump(lambda: len(self.ui.toasts) >= 2)
        self.assertIn("retrying for 60 s", self.ui.toasts[0])
        self.assertIn("Ctrl+Alt+R is already taken", self.ui.toasts[1])
        self.assertIn("[agent] record_hotkey", self.ui.toasts[1])
        self.assertIn("peep agent reload", self.ui.toasts[1])

    def test_agent_json_is_published(self):
        self.make_agent(code="abc123")
        info = self.actl.read()
        self.assertEqual(info["state"], "ready")
        self.assertEqual(info["code"], "abc123")
        self.assertEqual(info["pill"], "on")
        self.assertEqual(info["hotkeys"]["record"], "Ctrl+Alt+R registered")
        self.assertTrue(FakeListener.instances[0].started)

    def test_stop_command_while_idle_quits(self):
        a = self.make_agent()
        self.actl.send("stop")
        a.tick()
        self.assertTrue(self.ui.quit_called)
        self.assertTrue(FakeListener.instances[0].stopped)
        self.assertIsNone(self.actl.read())               # agent.json released

    def test_stop_command_while_recording_saves_first_without_a_dialog(self):
        a = self.make_agent()
        a.handle("record", CHROME)
        self.wait_live()
        self.actl.send("stop")
        a.tick()
        self.assertFalse(self.ui.quit_called)
        self.assertTrue(self.ui.pump(lambda: self.ui.quit_called))
        self.assertEqual(self.stop_reason, "agent-exit")
        self.assertEqual(self.ui.dialogs, [])
        self.assertEqual(len(self.entries()), 1)
        a.handle("record", CHROME)                        # ignored while exiting
        self.assertEqual(len(self.requests), 1)

    def test_shutdown_with_dialog_open_keeps_the_name(self):
        a = self.make_agent()
        self.record_and_stop()
        a.shutdown("test")
        self.assertTrue(self.ui.dialogs[0].closed)
        self.assertTrue(self.ui.quit_called)
        self.assertEqual(len(self.entries()), 1)

    def test_reload_reregisters_changed_hotkeys(self):
        new = c.from_mapping({"root": str(self.root), "agent": {"record_hotkey": "Ctrl+Shift+F9"}})
        a = self.make_agent(load_config=lambda: new)
        self.assertTrue(a.reload("test"))
        self.assertTrue(FakeListener.instances[0].stopped)
        self.assertEqual(FakeListener.instances[1].table["record"].text, "Ctrl+Shift+F9")
        self.assertIn("config reloaded (hotkeys re-registered)", self.ui.toasts)
        self.assertNotIn("config_error", {k for k, v in self.actl.read().items() if v})

    def test_reload_without_hotkey_change_keeps_the_listener(self):
        new = c.from_mapping({"root": str(self.root), "agent": {"pill_position": "bottom-left"}})
        a = self.make_agent(load_config=lambda: new)
        a.reload("test")
        self.assertEqual(len(FakeListener.instances), 1)
        self.assertEqual(a.cfg.agent.pill_position, "bottom-left")

    def test_invalid_config_is_reported_and_the_old_one_kept(self):
        def bad():
            raise c.ConfigError("unknown key(s) in [agent] of config.toml: hotkey")
        a = self.make_agent(load_config=bad)
        self.assertFalse(a.reload("test"))
        self.assertIs(a.cfg, self.cfg)
        self.assertIn("config not reloaded", self.ui.toasts[-1])
        self.assertIn("unknown key", self.actl.read()["config_error"])

    def test_config_edits_are_noticed_and_reload_command_works(self):
        mtimes = iter([1.0] + [2.0] * 50)
        calls = []
        a = self.make_agent(load_config=lambda: calls.append(1) or self.cfg, config_mtime=lambda: next(mtimes))
        for _ in range(agent_mod.CONFIG_CHECK_EVERY):
            a.tick()
        self.assertEqual(len(calls), 1)
        self.actl.send("reload")
        a.tick()
        self.assertEqual(len(calls), 2)

    def test_pill_that_cannot_be_excluded_is_disabled_not_shown(self):
        self.ui.pill_ok = False
        a = self.make_agent()
        self.assertIn("disabled", self.actl.read()["pill"])
        a.handle("record", CHROME)
        self.wait_live()
        a.tick()
        self.assertTrue(all(p is None for p in self.ui.pills))
        a.handle("record", None)
        self.ui.pump(lambda: a.session is None)

    def test_install_check_runs_off_thread_and_flags_stale_code(self):
        a = self.make_agent(install_check=lambda: {"state": "stale", "detail": "2 file(s) differ", "changed": ["a"]})
        self.assertTrue(self.ui.pump(lambda: a.install.get("state") == "stale"))
        self.assertEqual(self.actl.read()["install"]["state"], "stale")
        self.assertTrue(any("peep agent restart" in t for t in self.ui.toasts))


class PrefsTest(TempDirMixin, unittest.TestCase):
    def test_round_trip_and_bad_values(self):
        p = Prefs(self.tmp / "sub" / "agent-prefs.json")
        self.assertIsNone(p.last_collection())
        p.set_last_collection("bale")
        self.assertEqual(Prefs(self.tmp / "sub" / "agent-prefs.json").last_collection(), "bale")
        (self.tmp / "sub" / "agent-prefs.json").write_text('{"last_collection": "../etc"}')
        with self.assertLogs("peep.agent", "WARNING"):
            self.assertIsNone(p.last_collection())
        (self.tmp / "sub" / "agent-prefs.json").write_text("{torn")
        with self.assertLogs("peep.agent", "WARNING"):
            self.assertIsNone(p.last_collection())

    def test_next_collection_falls_back_to_the_default(self):
        cfg = c.from_mapping({"root": str(self.tmp), "default_collection": "scratch"})
        a = Agent(cfg, ui=FakeUi(), control=None, agent_control=None, prefs=Prefs(self.tmp / "p.json"),
                  recorder_factory=None, listener_factory=None)
        self.assertEqual(a.next_collection(), "scratch")
        a.prefs.set_last_collection("bale")
        self.assertEqual(a.next_collection(), "bale")


class AgentControlTest(TempDirMixin, unittest.TestCase):
    def ctl(self, alive):
        return AgentControl(self.tmp / "state", pid_alive=lambda pid: pid in alive)

    def test_claim_update_release(self):
        a = self.ctl({ME})
        a.claim({"state": "starting"})
        a.update(state="ready")
        self.assertEqual(a.live()["state"], "ready")
        a.release()
        self.assertIsNone(a.read())

    def test_second_live_agent_is_refused_stale_is_replaced(self):
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "agent.json").write_text(json.dumps({"pid": 4321}))
        with self.assertRaisesRegex(AgentAlreadyRunning, "pid 4321"):
            self.ctl({ME, 4321}).claim({})
        with self.assertLogs("peep.control", "WARNING") as logs:
            self.ctl({ME}).claim({})
        self.assertIn("agent.stale_pidfile", logs.output[0])

    def test_commands(self):
        a = self.ctl({ME})
        with self.assertRaisesRegex(LookupError, "not running"):
            a.send("stop")
        a.claim({})
        with self.assertRaises(ValueError):
            a.send("explode")
        a.send("reload")
        self.assertEqual(a.take_command(), "reload")
        self.assertIsNone(a.take_command())
        (self.tmp / "state" / "agent-command").write_text("dance\n")
        with self.assertLogs("peep.control", "WARNING"):
            self.assertIsNone(a.take_command())

    def test_leftover_command_is_cleared_at_claim(self):
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "agent-command").write_text("stop\n")
        a = self.ctl({ME})
        a.claim({})
        self.assertIsNone(a.take_command())

    def test_wait_helpers(self):
        a = self.ctl({ME})
        a.claim({"state": "starting"})
        ticks = iter(range(100))
        self.assertIsNone(a.wait_for(lambda i: i["state"] == "ready", 1.0, sleep=lambda s: None,
                                     clock=lambda: next(ticks) * 0.5))
        a.update(state="ready")
        self.assertEqual(a.wait_for(lambda i: i["state"] == "ready", 1.0, sleep=lambda s: None)["state"], "ready")
        self.assertTrue(self.ctl(set()).wait_gone(ME, 0.1, sleep=lambda s: None))
        ticks = iter(range(100))
        self.assertFalse(a.wait_gone(ME, 1.0, sleep=lambda s: None, clock=lambda: next(ticks) * 0.5))


class SingleInstanceTest(TempDirMixin, unittest.TestCase):
    def test_second_agent_exits_with_a_message_and_a_log_line(self):
        state = self.tmp / "home" / "state"
        AgentControl(state, lambda pid: True).claim({"state": "ready"})
        with mock.patch.dict(os.environ, {"PEEP_HOME": str(self.tmp / "home")}), \
                mock.patch("peep.winapi.SingleInstance", return_value=mock.Mock(acquired=False)), \
                mock.patch("sys.stderr") as err, \
                self.assertLogs("peep.agent", "WARNING") as logs:
            code = agent_mod.run_agent(c.Config())
        self.assertEqual(code, 1)
        self.assertIn("agent.second_instance", logs.output[0])
        self.assertIn(f"running_pid={ME}", logs.output[0])
        self.assertIn("already running", "".join(str(call) for call in err.mock_calls))
        self.assertEqual(AgentControl(state, lambda pid: True).read()["pid"], ME)   # the first one's file is untouched

    def test_live_pidfile_refuses_even_without_the_mutex(self):
        state = self.tmp / "home" / "state"
        state.mkdir(parents=True)
        (state / "agent.json").write_text(json.dumps({"pid": 999999}))
        with mock.patch.dict(os.environ, {"PEEP_HOME": str(self.tmp / "home")}), \
                mock.patch("peep.winapi.SingleInstance", return_value=mock.Mock(acquired=True)), \
                mock.patch("peep.winapi.pid_alive", return_value=True), \
                mock.patch("sys.stderr"), \
                self.assertLogs("peep.agent", "WARNING"):
            self.assertEqual(agent_mod.run_agent(c.Config()), 1)
        self.assertEqual(json.loads((state / "agent.json").read_text())["pid"], 999999)


if __name__ == "__main__":
    unittest.main()
