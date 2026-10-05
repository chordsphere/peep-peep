"""`peep mark` and `peep agent ...` through `cli.main`, against a temporary
data dir. The agent process itself is faked by writing agent.json the way
it would; spawning, PowerShell and pid liveness are mocked at the boundary."""

import json
import os
import threading
import time
import unittest
from pathlib import PureWindowsPath
from unittest import mock

from tests.test_cli import CliHarness
from peep import agentcli, lifecycle
from peep.control import AgentControl, Control

ME = os.getpid()


class AgentCliHarness(CliHarness):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / "home" / "state"
        self.dead = set()          # pids a test has "ended"
        self.alive = mock.patch("peep.winapi.pid_alive", side_effect=lambda pid: pid == ME and pid not in self.dead)
        self.alive.start()
        self.startup = mock.patch.object(lifecycle, "startup_dir", return_value=PureWindowsPath(str(self.tmp / "Startup")))
        self.startup.start()

    def tearDown(self):
        self.startup.stop()
        self.alive.stop()
        super().tearDown()

    def actl(self):
        return AgentControl(self.state, lambda pid: pid == ME)

    def fake_agent(self, **info):
        """agent.json as a running agent publishes it."""
        a = self.actl()
        a.claim({"state": "ready", "version": "0.1.0", "code": "c0de",
                 "hotkeys": {"record": "Ctrl+Alt+R registered", "mark": "Ctrl+Alt+M registered",
                             "discard": "Ctrl+Alt+X retrying: Ctrl+Alt+X is already taken by another app (winerror 1409)"},
                 "pill": "on", "config": "defaults", "config_loaded_at": "2026-10-05T09:00:00.000-04:00",
                 "install": {"state": "current", "detail": "matches the repo"}, **info})
        return a


class MarkCommandTest(AgentCliHarness, unittest.TestCase):
    def test_mark_with_nothing_recording(self):
        code, _, err = self.run_cli("mark")
        self.assertEqual(code, 1)
        self.assertIn("nothing is recording", err)

    def test_mark_writes_a_request(self):
        Control(self.state, lambda pid: True).claim({"capture": "x"})
        code, out, _ = self.run_cli("mark", "--label", "step 2")
        self.assertEqual(code, 0)
        self.assertIn("◆ mark requested (step 2)", out)
        req = json.loads(next(self.state.glob("mark-*.json")).read_text())
        self.assertEqual((req["source"], req["label"]), ("cli", "step 2"))


class AgentStatusTest(AgentCliHarness, unittest.TestCase):
    def test_not_running(self):
        code, out, _ = self.run_cli("agent", "status")
        self.assertEqual(code, 1)
        self.assertIn("agent: not running", out)
        self.assertIn("startup entry: absent", out)

    def test_running_shows_hotkeys_pill_config_install(self):
        self.fake_agent()
        with mock.patch.object(lifecycle, "exists", side_effect=lambda p: p.endswith("peep agent.lnk")):
            code, out, _ = self.run_cli("agent", "status")
        self.assertEqual(code, 0)
        self.assertIn(f"agent: running, pid {ME}", out)
        self.assertIn("hotkey discard  Ctrl+Alt+X retrying: Ctrl+Alt+X is already taken", out)
        self.assertIn("pill            on", out)
        self.assertIn("installed copy  current", out)
        self.assertIn("startup entry: present", out)

    def test_older_code_and_config_error_are_called_out(self):
        lines = agentcli.status_lines({"pid": 1, "code": "old", "config_error": "bad key", "hotkeys": {}},
                                      "new", None, False, {"status": "recording", "final": "x.mp4", "origin": "agent"})
        text = "\n".join(lines)
        self.assertIn("older than the installed copy: peep agent restart", text)
        self.assertIn("config ERROR    bad key", text)
        self.assertIn("recording: recording → x.mp4 (started by agent)", text)

    def test_json(self):
        self.fake_agent()
        code, out, _ = self.run_cli("agent", "status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["agent"]["code"], "c0de")


class AgentStartStopTest(AgentCliHarness, unittest.TestCase):
    def test_start_when_running_says_so(self):
        self.fake_agent()
        with mock.patch.object(lifecycle, "spawn_agent") as spawn:
            code, out, _ = self.run_cli("agent", "start")
        self.assertEqual(code, 0)
        self.assertIn("already running", out)
        spawn.assert_not_called()

    def test_start_spawns_and_waits_for_ready(self):
        def spawn(app, py):
            threading.Timer(0.2, self.fake_agent).start()      # the new agent publishes itself
            return ME

        with mock.patch.object(lifecycle, "spawn_agent", side_effect=spawn):
            code, out, err = self.run_cli("agent", "start", "--wait", "5")
        self.assertEqual(code, 0, err)
        self.assertIn(f"agent started (pid {ME})", out)
        self.assertIn("hotkey record", out)

    def test_start_that_never_gets_ready_points_at_the_log(self):
        with mock.patch.object(lifecycle, "spawn_agent", return_value=4242):
            code, _, err = self.run_cli("agent", "start", "--wait", "0.3")
        self.assertEqual(code, 1)
        self.assertIn("agent.log", err)

    def test_stop_sends_the_command_and_waits(self):
        a = self.fake_agent()

        def agent_loop():               # what the agent's tick does with the command
            for _ in range(200):
                if a.take_command() == "stop":
                    a.release()
                    self.dead.add(ME)       # the process exits
                    return
                time.sleep(0.02)

        t = threading.Thread(target=agent_loop)
        t.start()
        code, out, err = self.run_cli("agent", "stop", "--timeout", "5")
        t.join()
        self.assertEqual(code, 0, err)
        self.assertIn("agent stopped", out)

    def test_stop_when_not_running_is_fine(self):
        code, out, _ = self.run_cli("agent", "stop")
        self.assertEqual(code, 0)
        self.assertIn("not running", out)

    def test_reload_acknowledged_and_rejected(self):
        a = self.fake_agent()

        def agent_reloads(error=None):
            for _ in range(200):
                if a.take_command() == "reload":
                    a.update(config_loaded_at="2026-10-05T10:00:00.000-04:00", config_error=error)
                    return
                time.sleep(0.02)

        t = threading.Thread(target=agent_reloads)
        t.start()
        code, out, _ = self.run_cli("agent", "reload")
        t.join()
        self.assertEqual(code, 0)
        self.assertIn("config reloaded", out)
        t = threading.Thread(target=agent_reloads, args=("unknown key(s) in [agent]",))
        t.start()
        code, _, err = self.run_cli("agent", "reload")
        t.join()
        self.assertEqual(code, 1)
        self.assertIn("config not reloaded: unknown key", err)

    def test_install_writes_the_entry_then_starts(self):
        with mock.patch.object(lifecycle, "install_startup_entry", return_value="S\\peep agent.lnk") as inst, \
                mock.patch.object(lifecycle, "spawn_agent", side_effect=lambda app, py: self.fake_agent() and ME):
            code, out, err = self.run_cli("agent", "install")
        self.assertEqual(code, 0, err)
        self.assertIn("startup entry: S\\peep agent.lnk", out)
        self.assertIn("Ctrl+Alt+R record/stop", out)
        self.assertIn("agent started", out)
        self.assertEqual(inst.call_args.args[0], PureWindowsPath(str(self.tmp / "Startup")))

    def test_install_failure_is_one_message(self):
        with mock.patch.object(lifecycle, "install_startup_entry",
                               side_effect=lifecycle.LifecycleError("writing x failed (powershell exit 1): denied")):
            code, _, err = self.run_cli("agent", "install", "--no-start")
        self.assertEqual(code, 1)
        self.assertEqual(err.strip(), "peep: error: writing x failed (powershell exit 1): denied")

    def test_uninstall_removes_the_entry(self):
        with mock.patch.object(lifecycle, "remove_startup_entry", return_value="S\\peep agent.lnk"):
            code, out, _ = self.run_cli("agent", "uninstall")
        self.assertEqual(code, 0)
        self.assertIn("removed S\\peep agent.lnk", out)


class AgentRunLoggingTest(AgentCliHarness, unittest.TestCase):
    def test_agent_run_logs_to_its_own_file(self):
        import argparse
        self.assertEqual(agentcli.log_file_for(argparse.Namespace(command="agent", agent_command="run")), "agent.log")
        self.assertEqual(agentcli.log_file_for(argparse.Namespace(command="agent", agent_command="status")), "peep.log")
        self.assertEqual(agentcli.log_file_for(argparse.Namespace(command="ls")), "peep.log")
        with mock.patch("peep.agent.run_agent", return_value=0) as run:
            code, _, _ = self.run_cli("agent", "run")
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertTrue((self.tmp / "home" / "logs" / "agent.log").exists())

    def test_paths_lists_the_agent_log(self):
        code, out, _ = self.run_cli("paths")
        self.assertIn("agent log", out)


if __name__ == "__main__":
    unittest.main()
