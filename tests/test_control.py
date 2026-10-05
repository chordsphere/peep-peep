"""Control files (active.json / stop-request) and structured logging."""

import json
import logging
import os
import unittest

from tests import TempDirMixin
from peep import logsetup
from peep.control import AlreadyRecording, Control


class ControlTest(TempDirMixin, unittest.TestCase):
    def ctl(self, alive):
        return Control(self.tmp / "state", pid_alive=lambda pid: pid in alive)

    def test_claim_release(self):
        c = self.ctl({os.getpid()})
        c.claim({"capture": "C:/v/x.recording.mkv", "uid": "u"})
        info = c.read_active()
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(c.live_recording()["uid"], "u")
        c.release()
        self.assertIsNone(c.read_active())
        self.assertIsNone(c.live_recording())

    def test_second_live_claim_is_refused(self):
        c = self.ctl({os.getpid()})
        c.claim({"capture": "a"})
        with self.assertRaises(AlreadyRecording):
            c.claim({"capture": "b"})

    def test_stale_claim_is_replaced_and_logged(self):
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "active.json").write_text(json.dumps({"pid": 999999, "capture": "old"}))
        c = self.ctl({os.getpid()})
        with self.assertLogs("peep.control", "WARNING") as logs:
            c.claim({"capture": "new"})
        self.assertIn("control.stale_active", logs.output[0])
        self.assertEqual(c.read_active()["capture"], "new")

    def test_stop_request_round_trip(self):
        c = self.ctl({os.getpid()})
        with self.assertRaises(LookupError):
            c.request_stop()                      # nothing live
        c.claim({"capture": "a"})
        self.assertFalse(c.stop_requested())
        c.request_stop()
        self.assertTrue(c.stop_requested())
        self.assertFalse(c.stop_requested())      # consumed

    def test_leftover_stop_request_is_cleared_at_claim(self):
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "stop-request").write_text("old\n")
        c = self.ctl({os.getpid()})
        c.claim({"capture": "a"})
        self.assertFalse(c.stop_requested())

    def test_wait_finished(self):
        c = self.ctl({os.getpid()})
        c.claim({"capture": "a"})
        ticks = iter(range(100))
        self.assertFalse(c.wait_finished(os.getpid(), 1.0, sleep=lambda s: None,
                                         clock=lambda: next(ticks) * 0.5))
        c.release()
        self.assertTrue(c.wait_finished(os.getpid(), 1.0, sleep=lambda s: None))

    def test_unreadable_active_is_reported_not_trusted(self):
        (self.tmp / "state").mkdir()
        (self.tmp / "state" / "active.json").write_text("{not json")
        c = self.ctl(set())
        with self.assertLogs("peep.control", "WARNING"):
            self.assertIsNone(c.live_recording())


class LogFormatTest(TempDirMixin, unittest.TestCase):
    def test_kv_round_trip(self):
        argv = ["ffmpeg.exe", "-i", "audio=Microphone Array (6- Realtek XU)", "-f", "matroska"]
        msg = logsetup.kv("ffmpeg.argv", argv=argv, pid=42, path="C:\\Users\\chord\\x.mkv", note="two words")
        name, fields = logsetup.parse_kv(msg)
        self.assertEqual(name, "ffmpeg.argv")
        self.assertEqual(fields["argv"], argv)
        self.assertEqual(fields["pid"], 42)
        self.assertEqual(fields["path"], "C:\\Users\\chord\\x.mkv")
        self.assertEqual(fields["note"], "two words")

    def test_setup_writes_rotating_file(self):
        path = logsetup.setup_logging(self.tmp / "logs", "DEBUG", console=False)
        log = logging.getLogger("peep.test")
        logsetup.event(log, logging.INFO, "unit.test", value=1)
        for h in logging.getLogger("peep").handlers:
            h.flush()
        text = path.read_text(encoding="utf-8")
        self.assertIn("INFO peep.test unit.test value=1", text)
        for h in list(logging.getLogger("peep").handlers):
            logging.getLogger("peep").removeHandler(h)
            h.close()


if __name__ == "__main__":
    unittest.main()
