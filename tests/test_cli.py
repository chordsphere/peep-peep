"""Windows-side CLI: argument handling and the storage commands, run
through `cli.main` against a temporary data dir and storage root."""

import contextlib
import io
import json
import logging
import os
import threading
import time
import unittest
from unittest import mock

from tests import TempDirMixin
from tests.test_catalog import record
from peep import catalog, cli
from peep.control import Control


class CliHarness(TempDirMixin):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "profile" / "Videos" / "peep"
        self.env = mock.patch.dict(os.environ, {"PEEP_HOME": str(self.tmp / "home"),
                                                "USERPROFILE": str(self.tmp / "profile")})
        self.env.start()
        os.environ.pop("PEEP_CONFIG", None)

    def tearDown(self):
        self.env.stop()
        root = logging.getLogger("peep")
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        root.addHandler(logging.NullHandler())
        root.propagate = True
        super().tearDown()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()


class ConfigCommandTest(CliHarness, unittest.TestCase):
    """`peep config set/get/unset` (A.1) through the real CLI entry point."""

    def path(self):
        return self.tmp / "home" / "config.toml"

    def test_set_get_unset(self):
        code, out, err = self.run_cli("config", "set", "audio.offset_ms", "-120")    # a negative value parses
        self.assertEqual(code, 0, err)
        self.assertIn("audio.offset_ms: 0 -> -120 (uncommented the template line)", out)
        self.assertIn("[created", out)
        code, out, _ = self.run_cli("config", "get", "audio.offset_ms")
        self.assertEqual((code, out.strip()), (0, "-120"))
        code, out, _ = self.run_cli("config", "get")
        self.assertIn("audio.offset_ms", out)
        self.assertRegex(out, r"audio.offset_ms\s+= -120   # set in config.toml")
        self.assertRegex(out, r'audio.sources\s+= "system"\n')
        code, out, _ = self.run_cli("config", "unset", "audio.offset_ms")
        self.assertIn("audio.offset_ms: -120 -> 0 (back to the commented default)", out)
        self.assertNotIn("offset_ms", __import__("tomllib").loads(self.path().read_text())["audio"])

    def test_set_refuses_bad_values_and_keys(self):
        code, _, err = self.run_cli("config", "set", "audio.sources", "speakers")
        self.assertEqual(code, 1)
        self.assertIn("audio.sources must be one of", err)
        self.assertFalse(self.path().exists())                    # refused before anything was written
        code, _, err = self.run_cli("config", "set", "audio.sorces", "mic")
        self.assertIn("did you mean audio.sources", err)
        code, _, err = self.run_cli("config", "set", "audio.sources")
        self.assertEqual(code, 2)
        code, _, err = self.run_cli("config", "unset")
        self.assertEqual(code, 2)

    def test_set_repairs_a_broken_file(self):
        self.path().parent.mkdir(parents=True)
        self.path().write_text("[audio]\nsources = 'speakers'\n")
        code, _, err = self.run_cli("ls")
        self.assertEqual(code, 1)
        code, out, err = self.run_cli("config", "set", "audio.sources", "both")
        self.assertEqual(code, 0, err)
        self.assertIn("continuing so `config` can repair the file", err)
        self.assertEqual(self.run_cli("ls")[0], 0)

    def test_path(self):
        code, out, _ = self.run_cli("config", "path")
        self.assertEqual(out.strip(), str(self.path()))


class StorageCommandsTest(CliHarness, unittest.TestCase):
    def test_ls_empty(self):
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, 0)
        self.assertIn("no recordings yet", out)

    def test_ls_lists_filters_and_json(self):
        record(self.root, "inbox", "2026-10-05-first")
        record(self.root, "bale", "2026-10-05-second")
        record(self.root, "inbox", "2026-10-05-broken", ext=".mkv", event="failed")
        code, out, _ = self.run_cli("ls")
        self.assertEqual(code, 0)
        self.assertIn("inbox/2026-10-05-first.mp4", out)
        self.assertIn("bale/2026-10-05-second.mp4", out)
        self.assertNotIn("broken", out)
        self.assertIn("0:12", out)                                   # 12.5 s rounds to 0:12
        _, out, _ = self.run_cli("ls", "--collection", "bale")
        self.assertNotIn("first", out)
        _, out, _ = self.run_cli("ls", "--all")
        self.assertIn("[failed]", out)
        _, out, _ = self.run_cli("ls", "--json", "-n", "1")
        data = json.loads(out)
        self.assertEqual([d["file"] for d in data], ["bale/2026-10-05-second.mp4"])

    def test_rename_last(self):
        record(self.root, "inbox", "2026-10-05-terminal-peep-peep")
        code, out, _ = self.run_cli("rename", "last", "bale pack demo")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "inbox" / "2026-10-05-bale-pack-demo.mp4").exists())
        self.assertIn("2026-10-05-bale-pack-demo.mp4", out)

    def test_rename_errors_are_messages_not_tracebacks(self):
        code, _, err = self.run_cli("rename", "last", "x")
        self.assertEqual(code, 1)
        self.assertIn("peep: error: no recordings", err)
        record(self.root, "inbox", "2026-10-05-a")
        code, _, err = self.run_cli("rename", "last", "x", "--collection", "../evil")
        self.assertEqual(code, 1)
        self.assertIn("collection", err)

    def test_open_missing_file(self):
        record(self.root, "inbox", "2026-10-05-a")
        (self.root / "inbox" / "2026-10-05-a.mp4").unlink()
        code, _, err = self.run_cli("open", "last")
        self.assertEqual(code, 1)
        self.assertIn("missing", err)

    def test_open_uses_default_handler(self):
        record(self.root, "inbox", "2026-10-05-a")
        with mock.patch("peep.winapi.open_with_default") as opener:
            code, out, _ = self.run_cli("open")
        self.assertEqual(code, 0)
        opener.assert_called_once_with(str(self.root / "inbox" / "2026-10-05-a.mp4"))

    def test_config_show_and_init(self):
        code, out, _ = self.run_cli("config")
        self.assertEqual(code, 0)
        self.assertIn('"default_collection": "inbox"', out)
        code, out, _ = self.run_cli("config", "--init")
        self.assertEqual(code, 0)
        self.assertTrue((self.tmp / "home" / "config.toml").exists())
        code, _, err = self.run_cli("config", "--init")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

    def test_invalid_config_is_reported(self):
        (self.tmp / "home").mkdir(parents=True)
        (self.tmp / "home" / "config.toml").write_text("[video]\nfsp = 3\n")
        code, _, err = self.run_cli("ls")
        self.assertEqual(code, 1)
        self.assertIn("unknown key", err)

    def test_paths(self):
        code, out, _ = self.run_cli("paths")
        self.assertEqual(code, 0)
        self.assertIn(str(self.root / "catalog.jsonl"), out)

    def test_usage_error_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["frobnicate"])
        self.assertEqual(cm.exception.code, 2)

    def test_log_records_command_and_exit(self):
        self.run_cli("ls")
        text = (self.tmp / "home" / "logs" / "peep.log").read_text(encoding="utf-8")
        self.assertIn("cli.start command=ls", text)
        self.assertIn("cli.exit command=ls exit_code=0", text)


class StopCommandTest(CliHarness, unittest.TestCase):
    def control(self):
        from peep import winapi
        return Control(self.tmp / "home" / "state", winapi.pid_alive)

    def test_stop_with_nothing_recording(self):
        code, _, err = self.run_cli("stop")
        self.assertEqual(code, 1)
        self.assertIn("nothing is recording", err)

    def test_stop_no_wait_writes_request(self):
        self.control().claim({"capture": "C:/v/x.recording.mkv", "final": "C:/v/x.mp4", "uid": "u"})
        code, out, _ = self.run_cli("stop", "--no-wait")
        self.assertEqual(code, 0)
        self.assertIn("x.mp4", out)
        self.assertTrue((self.tmp / "home" / "state" / "stop-request").exists())

    def test_stop_waits_for_the_recorder_and_reports_the_file(self):
        ctl = self.control()
        ctl.claim({"capture": "c", "final": "f.mp4", "uid": "u1"})

        def fake_recorder():   # what the recorder does on seeing the request
            deadline = time.monotonic() + 20            # bounded: never hang the suite
            while not ctl.stop_requested():
                if time.monotonic() > deadline:
                    return
                time.sleep(0.02)
            catalog.Catalog(self.root).append({"event": "recorded", "uid": "u1", "collection": "inbox",
                                               "file": "inbox/2026-10-05-x.mp4", "title": "x",
                                               "created": "t", "duration_s": 3.0})
            ctl.release()

        t = threading.Thread(target=fake_recorder)
        t.start()
        code, out, _ = self.run_cli("stop", "--timeout", "10")
        t.join(25)
        self.assertEqual(code, 0, out)
        self.assertIn("saved", out)
        self.assertIn("2026-10-05-x.mp4", out)


class RecCommandTest(CliHarness, unittest.TestCase):
    def test_rec_builds_the_request(self):
        seen = {}

        class FakeRecorder:
            def __init__(self, cfg, control, status):
                pass

            def record(self, req, stop_event):
                seen["req"] = req
                from peep.recorder import RecordResult
                return RecordResult(True, "saved X (1.0s)")

        with mock.patch("peep.recorder.Recorder", FakeRecorder):
            code, out, _ = self.run_cli("rec", "my demo", "-c", "bale", "--no-flash", "--no-mic",
                                        "--scale", "1920x1200", "--encoder", "x264", "--fps", "60",
                                        "--stdin-stop", "off")
        self.assertEqual(code, 0)
        r = seen["req"]
        self.assertEqual((r.slug, r.collection, r.flash, r.audio_sources, r.scale, r.pipeline, r.fps),
                         ("my demo", "bale", False, "system", "1920x1200", "x264", 60))   # --no-mic: system stays
        self.assertIn("saved X", out)

    def test_rec_audio_choices(self):
        seen = []

        class FakeRecorder:
            def __init__(self, cfg, control, status):
                pass

            def record(self, req, stop_event):
                seen.append(req.audio_sources)
                from peep.recorder import RecordResult
                return RecordResult(True, "saved X (1.0s)")

        with mock.patch("peep.recorder.Recorder", FakeRecorder):
            for args in (("--audio", "both"), ("--audio", "both", "--no-mic"), ("--audio", "mic", "--no-mic"),
                         ("--audio", "none"), ()):
                self.assertEqual(self.run_cli("rec", "--stdin-stop", "off", *args)[0], 0)
        self.assertEqual(seen, ["both", "system", "none", "none", "system"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["rec", "--audio", "speakers"])

    def test_rec_rejects_bad_scale_before_recording(self):
        code, _, err = self.run_cli("rec", "--scale", "huge", "--stdin-stop", "off")
        self.assertEqual(code, 1)
        self.assertIn("scale must be", err)

    def test_watch_stdin_line_and_eof(self):
        for data in (b"stop\n", b""):
            ev = threading.Event()
            cli.watch_stdin(ev, io.BytesIO(data)).join(2)
            self.assertTrue(ev.is_set(), data)

    def test_status_output_survives_a_vanished_terminal(self):
        class Dead:
            def write(self, _):
                raise OSError(5, "Input/output error")

            def flush(self):
                raise OSError(5, "Input/output error")

        with mock.patch("sys.stdout", Dead()), self.assertLogs("peep.cli", "WARNING") as logs:
            cli._out("■ stopping…")          # must not raise
        self.assertIn("stdout.write_failed", logs.output[0])
        cli._out.failed = False

    def test_format_duration(self):
        self.assertEqual(cli.format_duration(0), "0:00")
        self.assertEqual(cli.format_duration(61.4), "1:01")
        self.assertEqual(cli.format_duration(None), "?")


if __name__ == "__main__":
    unittest.main()
