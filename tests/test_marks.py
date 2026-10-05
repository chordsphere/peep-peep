"""Marks end to end (control files -> recorder -> sidecar), and the other
session-B additions to the recorder: foreground passed in at hotkey time,
origin, the active.json status field, and the stop reason."""

import datetime as dt
import json
import os
import threading
import time
import unittest

from tests import TempDirMixin
from tests.test_recorder import RecorderHarness
from peep import catalog
from peep.control import Control
from peep.recorder import RecordRequest, _seconds_between
from peep.winapi import ForegroundInfo

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


class MarkControlTest(TempDirMixin, unittest.TestCase):
    def ctl(self):
        return Control(self.tmp / "state", pid_alive=lambda pid: pid == os.getpid())

    def test_mark_needs_a_live_recording(self):
        with self.assertRaisesRegex(LookupError, "nothing is recording"):
            self.ctl().request_mark("cli")

    def test_requests_are_one_file_each_and_consumed_in_order(self):
        c = self.ctl()
        c.claim({"capture": "x"})
        first = c.request_mark("hotkey")
        c.request_mark("cli", "intro done")
        files = sorted(p.name for p in (self.tmp / "state").glob("mark-*.json"))
        self.assertEqual(len(files), 2)                       # two presses, two marks
        taken = c.take_marks()
        self.assertEqual([(m["source"], m["label"]) for m in taken], [("hotkey", ""), ("cli", "intro done")])
        self.assertEqual(taken[0]["requested_at"], first["requested_at"])
        self.assertEqual(c.take_marks(), [])                  # consumed
        self.assertFalse(list((self.tmp / "state").glob("mark-*")))

    def test_unreadable_request_is_logged_and_dropped(self):
        c = self.ctl()
        c.claim({"capture": "x"})
        (self.tmp / "state" / "mark-00000000000000000001-1.json").write_text("{torn")
        c.request_mark("cli")
        with self.assertLogs("peep.control", "WARNING") as logs:
            taken = c.take_marks()
        self.assertEqual(len(taken), 1)
        self.assertIn("control.mark_unreadable", logs.output[0])
        self.assertFalse(list((self.tmp / "state").glob("mark-*")))

    def test_leftover_marks_never_reach_the_next_recording(self):
        c = self.ctl()
        c.claim({"capture": "a"})
        c.request_mark("cli")
        c.release()
        self.assertFalse(list((self.tmp / "state").glob("mark-*")))
        (self.tmp / "state" / "mark-00000000000000000002-9.json").write_text('{"source": "stale"}')
        c.claim({"capture": "b"})
        self.assertEqual(c.take_marks(), [])

    def test_update_active_only_touches_our_own_file(self):
        c = self.ctl()
        c.claim({"capture": "a", "status": "starting"})
        c.update_active(status="recording")
        self.assertEqual(c.read_active()["status"], "recording")
        (self.tmp / "state" / "active.json").write_text(json.dumps({"pid": 1, "status": "x"}))
        with self.assertLogs("peep.control", "WARNING"):
            c.update_active(status="stopping")
        self.assertEqual(c.read_active()["status"], "x")

    def test_seconds_between(self):
        self.assertEqual(_seconds_between("2026-10-05T10:00:00.000-04:00", "2026-10-05T10:00:12.345-04:00"), 12.345)
        self.assertIsNone(_seconds_between(None, "2026-10-05T10:00:00-04:00"))
        self.assertIsNone(_seconds_between("garbage", "2026-10-05T10:00:00-04:00"))


class RecorderMarksTest(RecorderHarness, unittest.TestCase):
    def record_with(self, actions, req=None, cfg=None, flasher=None):
        """Run a recording; `actions(control)` runs once it is live, then it stops."""
        rec = self.recorder(cfg=cfg, flasher=flasher)
        ev = threading.Event()
        seen = {}

        def driver():
            for _ in range(500):
                info = self.control.read_active()
                if info and info.get("status") == "recording":
                    break
                time.sleep(0.01)
            try:
                seen["active"] = dict(self.control.read_active() or {})
                actions(self.control)
                time.sleep(0.35)
            finally:                       # a failing action must not leave the recording running
                ev.reason = "hotkey"
                ev.set()

        t = threading.Thread(target=driver)
        t.start()
        try:
            res = rec.record(req or RecordRequest(), ev)
        finally:
            t.join()
        return res, seen

    def test_marks_flash_cyan_and_land_in_the_sidecar(self):
        def two_marks(ctl):
            ctl.request_mark("hotkey")
            time.sleep(0.25)
            ctl.request_mark("cli", "step 2")

        res, _ = self.record_with(two_marks)
        self.assertTrue(res.ok, res.message)
        sc = catalog.read_sidecar(res.sidecar_path)
        marks = sc["marks"]
        self.assertEqual(len(marks), 2)
        self.assertEqual([m["source"] for m in marks], ["hotkey", "cli"])
        self.assertEqual(marks[1]["label"], "step 2")
        self.assertEqual(self.flasher.calls, [("#FF00FF", 200), ("#00FFFF", 200), ("#00FFFF", 200),
                                              ("#00FF00", 200)])
        start = sc["flash"]["start"]["since_ffmpeg_start_s"]
        stop = sc["flash"]["stop"]["since_ffmpeg_start_s"]
        for m in marks:
            self.assertEqual(m["t"], m["since_ffmpeg_start_s"])                 # A's reserved key, filled
            self.assertTrue(start <= m["t"] <= stop, (start, m["t"], stop))
            self.assertEqual(m["flash"]["color"], "#00FFFF")
            self.assertGreaterEqual(m["flash"]["since_ffmpeg_start_s"], m["t"])  # flash follows the press
            self.assertLessEqual(m["t"], m["consumed_s"])
        self.assertLess(marks[0]["t"], marks[1]["t"])
        self.assertTrue(any("◆ mark 2" in s for s in self.status))

    def test_t_is_the_press_time_not_the_poll_time(self):
        def backdated(ctl):
            # A request written 2 s "after" ffmpeg started, consumed later: t must say 2.0.
            started = dt.datetime.fromisoformat(ctl.read_active()["ffmpeg_started_at"])
            stamp = (started + dt.timedelta(seconds=2)).isoformat(timespec="milliseconds")
            (ctl.dir / "mark-00000000000000000001-1.json").write_text(
                json.dumps({"requested_at": stamp, "source": "test", "label": ""}))

        res, _ = self.record_with(backdated)
        self.assertEqual(catalog.read_sidecar(res.sidecar_path)["marks"][0]["t"], 2.0)

    def test_marks_without_flash_have_no_flash_entry(self):
        res, _ = self.record_with(lambda ctl: ctl.request_mark("cli"), req=RecordRequest(flash=False))
        m = catalog.read_sidecar(res.sidecar_path)["marks"][0]
        self.assertIsNone(m["flash"])
        self.assertEqual(self.flasher.calls, [])

    def test_mark_pressed_just_before_stop_is_kept(self):
        rec = self.recorder()
        ev = threading.Event()

        def driver():
            while not (self.control.read_active() or {}).get("status") == "recording":
                time.sleep(0.01)
            self.control.request_mark("hotkey")
            ev.set()                                   # same instant: the final sweep must catch it

        t = threading.Thread(target=driver)
        t.start()
        res = rec.record(RecordRequest(), ev)
        t.join()
        self.assertEqual(len(catalog.read_sidecar(res.sidecar_path)["marks"]), 1)

    def test_marks_are_persisted_while_recording(self):
        seen = {}

        def mark_then_peek(ctl):
            ctl.request_mark("cli")
            path = ctl.read_active()["sidecar"]
            for _ in range(100):
                sc = catalog.read_sidecar(path)
                if sc["marks"]:
                    seen["marks"] = sc["marks"]
                    seen["status"] = sc["status"]
                    return
                time.sleep(0.02)

        self.record_with(mark_then_peek)
        self.assertEqual(seen["status"], "recording")          # on disk before the recording ended
        self.assertEqual(len(seen["marks"]), 1)

    def test_foreground_origin_status_and_stop_reason(self):
        fg = ForegroundInfo("Pull requests · peep-peep - Google Chrome", CHROME)
        res, seen = self.record_with(lambda ctl: None,
                                     req=RecordRequest(collection="bale", foreground=fg, origin="agent"))
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertEqual(sc["slug"], "chrome-pull-requests-peep-peep")    # from the passed-in window, not the terminal
        self.assertEqual(sc["foreground"]["process"], "chrome.exe")
        self.assertEqual(sc["timeline"]["origin"], "agent")
        self.assertEqual(sc["timeline"]["stop_reason"], "hotkey")
        self.assertEqual(seen["active"]["origin"], "agent")
        self.assertEqual(seen["active"]["status"], "recording")
        self.assertIn("recording_since", seen["active"])
        self.assertEqual(res.duration_s, sc["duration_s"])

    def test_terminal_recording_is_unchanged(self):
        res = self.record()
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertEqual(sc["timeline"]["origin"], "terminal")
        self.assertEqual(sc["timeline"]["stop_reason"], "terminal")
        self.assertEqual(sc["marks"], [])
        self.assertEqual(sc["slug"], "terminal-peep-peep")


if __name__ == "__main__":
    unittest.main()
