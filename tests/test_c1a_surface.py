"""Session C1a's user-facing surface: the agent's pause / take / retake
chords, the pill's PAUSED and take states, the dialog's status line and
"saves as" preview, the WSL commands (`peep pause|resume|take|retake`, in
the Windows CLI and the shim), the new chords and fiducial settings, and the
corner patch geometry. Also the reproduction of the architect's reported
overwrite, end to end through the agent and the dialog."""

import datetime as dt
import json
import threading
import time
import unittest

from tests.test_agent import CHROME, AgentHarness
from tests.test_cli import CliHarness
from tests.test_shim import CFG, DispatchTest, shim
from peep import config as c, dialog, hotkeys as hk, pill
from peep.control import Control
from peep.flash import FlashRecord, patch_corner, patch_rect
from peep.recorder import RecordResult
from peep.winapi import ForegroundInfo
from unittest import mock

BRAVE = ForegroundInfo("Watch Black Swan Online Free - Brave",
                       r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe")


class AgentEventChordTest(AgentHarness, unittest.TestCase):
    def event_requests(self):
        out = []
        for p in sorted(self.state.glob("event-*.json")):
            out.append(json.loads(p.read_text()))
        return out

    def test_chords_write_requests_stamped_at_the_press(self):
        a = self.make_agent()
        a.handle("record", CHROME)
        self.wait_live()
        a.handle("take", None, 123.25, "2026-10-07T10:00:00.000-04:00")
        a.handle("retake", None, 125.5, "2026-10-07T10:00:02.250-04:00")
        a.handle("pause", None, 130.0, "2026-10-07T10:00:06.750-04:00")
        reqs = self.event_requests()
        self.assertEqual([(r["kind"], r["source"], r["requested_qpc"]) for r in reqs],
                         [("take", "hotkey", 123.25), ("retake", "hotkey", 125.5), ("pause-toggle", "hotkey", 130.0)])
        self.assertEqual(reqs[0]["requested_at"], "2026-10-07T10:00:00.000-04:00")
        a.handle("record", None)
        self.assertTrue(self.ui.pump(lambda: a.session is None))

    def test_on_hotkey_stamps_the_instant_it_arrives(self):
        a = self.make_agent()
        before = time.perf_counter()
        a.on_hotkey("take", None)
        fn, args = self.ui.q.get_nowait()
        self.assertEqual(fn, a.handle)
        self.assertEqual(args[0], "take")
        self.assertGreaterEqual(args[2], before)
        self.assertIsInstance(args[3], str)

    def test_nothing_recording_says_so(self):
        a = self.make_agent()
        for action in ("take", "retake", "pause"):
            a.handle(action, None)
        self.assertEqual(self.ui.toasts, [f"nothing is recording, so there is nothing to {x}"
                                          for x in ("take", "retake", "pause")])
        self.assertEqual(self.event_requests(), [])

    def test_terminal_recordings_get_the_chords_too(self):
        a = self.make_agent()
        self.control.claim({"uid": "u", "origin": "terminal", "status": "recording", "final": "f"})
        a.handle("take", None, 1.0, "t")
        self.assertEqual(self.event_requests()[0]["kind"], "take")
        self.control.release()

    def test_pill_shows_paused_and_take_state(self):
        a = self.make_agent()
        self.control.claim({"uid": "u", "origin": "terminal", "status": "paused", "final": "f",
                            "captured_s": 72.4, "takes": 2, "take_open": False})
        a.tick()
        self.assertEqual(self.ui.pills[-1], "❚❚ PAUSED  01:12  ○ 2 takes")
        now = dt.datetime.now().astimezone()
        self.control.update_active(status="recording", captured_s=72.4, takes=3, take_open=True,
                                   recording_since=(now - dt.timedelta(seconds=10)).isoformat())
        a.now = lambda: now
        a.tick()
        self.assertEqual(self.ui.pills[-1], "● 01:22  ◉ take 3")
        self.control.update_active(status="pausing")
        a.tick()
        self.assertEqual(self.ui.pills[-1], "❚❚ pausing…")
        self.control.release()

    def test_dialog_status_line_has_takes_and_segments(self):
        a = self.make_agent()
        self.record_and_stop()
        media = self.root / "inbox" / "2026-10-05-chrome-pull-requests-peep-peep.mp4"
        res = RecordResult(True, "saved", uid=self.entries()[-1].uid, media_path=media, duration_s=302.0,
                           segments=2, size_bytes=54 * 1024 * 1024,
                           summary={"segments": 2, "takes": 3, "kept_s": 134.0, "total_s": 302.0, "whole": False})
        a._open_dialog(a.session or type("S", (), {"collection": "inbox"})(), res)
        status = self.ui.dialogs[-1].model.status
        self.assertTrue(status.startswith("05:02  ·  3 takes  ·  02:14 kept of 05:02  ·  2 segments  ·  54.0 MB  ·  "),
                        status)


class ArchitectReproductionTest(AgentHarness, unittest.TestCase):
    """The laptop's evidence (probe, 2026-10-07): in `movies`, 21:55 saved
    2026-10-06-brave-watch-black-swan-online-free.mp4; the 21:56 recording of the
    same tab was allocated ...-free-2 correctly, but its dialog prefilled
    "brave-watch-black-swan-online-free" (the counter dropped), the name of the
    file beside it. Saving looked like an overwrite, so at 22:25 the architect
    typed "...-free2" by hand. No bytes were ever overwritten (the catalog and
    every sidecar uid agree). Here the same sequence through the agent."""

    def test_the_dialog_shows_the_real_name_and_what_enter_will_save(self):
        self.prefs.set_last_collection("movies")
        self.make_agent()
        self.record_and_stop(BRAVE)
        self.assertIsNone(self.ui.dialogs[0].resolve(dialog.KEEP))
        first = self.root / "movies" / "2026-10-05-brave-watch-black-swan-online-free.mp4"
        first_bytes = first.read_bytes()
        self.record_and_stop(BRAVE)
        dlg = self.ui.dialogs[1]
        # before C1a this was "brave-watch-black-swan-online-free": another recording's name
        self.assertEqual(dlg.model.name, "brave-watch-black-swan-online-free-2")
        self.assertEqual(dlg.model.preview(dlg.model.name, "movies"),
                         "keeps its name: movies/2026-10-05-brave-watch-black-swan-online-free-2.mp4")
        self.assertEqual(dlg.model.preview("brave watch black swan online free", "movies"),
                         "keeps its name: movies/2026-10-05-brave-watch-black-swan-online-free-2.mp4   "
                         "(2026-10-05-brave-watch-black-swan-online-free is taken)")
        self.assertEqual(dlg.model.preview("Black Swan", "inbox"),
                         "saves as inbox/2026-10-05-black-swan.mp4")
        self.assertIn("cannot be used", dlg.model.preview("nul", "movies"))
        # Enter on the old, counter-less prefill: no overwrite, the counter stays, and it says so
        self.assertIsNone(dlg.resolve(dialog.SAVE, "brave-watch-black-swan-online-free", "movies"))
        self.assertEqual(first.read_bytes(), first_bytes)
        self.assertEqual(sorted(p.name for p in (self.root / "movies").iterdir()),
                         ["2026-10-05-brave-watch-black-swan-online-free-2.json",
                          "2026-10-05-brave-watch-black-swan-online-free-2.mp4",
                          "2026-10-05-brave-watch-black-swan-online-free.json",
                          "2026-10-05-brave-watch-black-swan-online-free.mp4"])
        # a different free name, and one that is taken: the toast shows the final name
        self.record_and_stop(BRAVE)
        self.assertIsNone(self.ui.dialogs[2].resolve(dialog.SAVE, "Brave Watch Black Swan Online Free", "movies"))
        self.assertTrue(any("saved movies/2026-10-05-brave-watch-black-swan-online-free-3.mp4" in t
                            for t in self.ui.toasts), self.ui.toasts)
        self.assertEqual(len({e.file for e in self.entries()}), 3)


class PillAndDialogHelpersTest(unittest.TestCase):
    def test_pill_text(self):
        self.assertEqual(pill.pill_text("recording", 12.7), "● 00:12")                # B's pill, unchanged
        self.assertEqual(pill.pill_text("recording", 12.7, 1, True), "● 00:12  ◉ take 1")
        self.assertEqual(pill.pill_text("recording", 12.7, 2, False), "● 00:12  ○ 2 takes")
        self.assertEqual(pill.pill_text("paused", 61), "❚❚ PAUSED  01:01")
        self.assertEqual(pill.pill_text("resuming", None), "● resuming…")
        for state in ("paused", "pausing", "resuming"):
            self.assertIn(state, pill.COLORS)
        self.assertNotEqual(pill.COLORS["paused"], pill.COLORS["recording"])

    def test_status_line(self):
        self.assertEqual(dialog.status_line(12.0, 2048, "p"), "00:12  ·  2 KB  ·  p")      # B's line, unchanged
        self.assertEqual(dialog.status_line(12.0, 2048, "p", "1 take  ·  00:05 kept of 00:12"),
                         "00:12  ·  1 take  ·  00:05 kept of 00:12  ·  2 KB  ·  p")

    def test_patch_geometry(self):
        self.assertEqual(patch_rect("bottom-left", 200, 2560, 1600), (0, 1400, 200, 200))
        self.assertEqual(patch_rect("top-right", 200, 2560, 1600, 24), (2336, 24, 200, 200))
        self.assertEqual(patch_rect("bottom-right", 5000, 300, 200), (100, 0, 200, 200))   # clamped to the screen
        self.assertEqual(patch_corner("auto", "top-right"), "bottom-left")
        self.assertEqual(patch_corner("auto", "bottom-left"), "top-right")
        self.assertEqual(patch_corner("auto", "top-center"), "bottom-left")
        self.assertEqual(patch_corner("top-left", "top-left"), "top-left")                 # explicit wins

    def test_patch_record_in_the_sidecar(self):
        rec = FlashRecord("#0000FF", 200, "t", 10.0, 10.2, True, 5.0, style="patch", rect=(0, 1400, 200, 200),
                          screen=(2560, 1600))
        side = rec.to_sidecar(9.0)
        self.assertEqual((side["style"], side["rect"], side["screen"]),
                         ("patch", {"x": 0, "y": 1400, "w": 200, "h": 200}, {"w": 2560, "h": 1600}))
        full = FlashRecord("#FF00FF", 200, "t", 10.0, 10.2, True, 5.0).to_sidecar(9.0)
        self.assertEqual(set(full), {"color", "duration_ms", "shown", "shown_at", "since_ffmpeg_start_s",
                                     "actual_ms", "shown_qpc"})                              # A's keys exactly


class ChordAndConfigTest(unittest.TestCase):
    def test_numpad_and_function_keys(self):
        self.assertEqual(hk.parse_chord("Ctrl+Alt+Num1").vk, 0x61)
        self.assertEqual(hk.parse_chord("ctrl+alt+numpad0").text, "Ctrl+Alt+Num0")
        self.assertEqual(hk.parse_chord("Alt+NumAdd").vk, 0x6B)
        self.assertEqual(hk.parse_chord("Ctrl+Alt+F12").vk, 0x7B)
        f13 = hk.parse_chord("F13")
        self.assertEqual((f13.mods, f13.vk, f13.text), (0, 0x7C, "F13"))          # nothing types F13-F24
        self.assertEqual(hk.parse_chord("f24").vk, 0x87)
        for bare in ("F12", "Num5", "T"):
            with self.subTest(bare=bare), self.assertRaisesRegex(hk.HotkeyError, "only F13-F24"):
                hk.parse_chord(bare)

    def test_new_chords_are_configurable_and_conflicts_refused(self):
        cfg = c.from_mapping({"agent": {"take_hotkey": "Ctrl+Alt+Num1", "retake_hotkey": "Ctrl+Alt+Num2",
                                        "pause_hotkey": "F13", "debounce_ms": 600}})
        table = hk.table_from_agent_config(cfg.agent)
        self.assertEqual((table["take"].text, table["retake"].text, table["pause"].text),
                         ("Ctrl+Alt+Num1", "Ctrl+Alt+Num2", "F13"))
        self.assertEqual(cfg.agent.debounce_ms, 600)
        with self.assertRaisesRegex(c.ConfigError, "retake_hotkey and take_hotkey|take_hotkey and retake_hotkey"):
            c.from_mapping({"agent": {"retake_hotkey": "Ctrl+Alt+T"}})
        with self.assertRaisesRegex(c.ConfigError, "debounce_ms"):
            c.from_mapping({"agent": {"debounce_ms": -1}})

    def test_fiducial_colours_must_stay_apart(self):
        with self.assertRaisesRegex(c.ConfigError, "take_open_color #0000FF and flash.take_close_color #0000F0"):
            c.from_mapping({"flash": {"take_close_color": "#0000F0"}})
        with self.assertRaisesRegex(c.ConfigError, "too alike"):
            c.from_mapping({"flash": {"retake_color": "#FF00EE"}})          # magenta-ish: the start flash
        cfg = c.from_mapping({"flash": {"retake_color": "#FF8000"}})
        self.assertEqual(cfg.flash.retake_color, "#FF8000")

    def test_patch_settings_are_validated(self):
        for data, why in (({"flash": {"mark_style": "strobe"}}, "mark_style"),
                          ({"flash": {"patch_corner": "middle"}}, "patch_corner"),
                          ({"flash": {"patch_size_px": 8}}, "patch_size_px"),
                          ({"flash": {"patch_margin_px": -2}}, "patch_margin_px")):
            with self.subTest(data=data), self.assertRaisesRegex(c.ConfigError, why):
                c.from_mapping(data)

    def test_template_documents_the_new_keys(self):
        for key in ("pause_hotkey", "take_hotkey", "retake_hotkey", "debounce_ms", "take_open_color",
                    "take_close_color", "retake_color", "mark_style", "patch_size_px", "patch_corner",
                    "patch_margin_px"):
            self.assertIn(f"# {key} = ", c.TEMPLATE)


class PauseTakeCommandTest(CliHarness, unittest.TestCase):
    """`peep pause|resume|take|retake` through cli.main, against a stand-in recorder
    thread that consumes the request and publishes its status the way the real one does."""

    def control(self):
        from peep import winapi
        return Control(self.tmp / "home" / "state", winapi.pid_alive)

    def recorder(self, ctl, reply):
        def run():
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                reqs = ctl.take_requests()
                if reqs:
                    reply(ctl, reqs[0])
                    return
                time.sleep(0.02)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def test_nothing_recording(self):
        for cmd in ("pause", "resume", "take", "retake"):
            code, _, err = self.run_cli(cmd)
            self.assertEqual(code, 1)
            self.assertIn("nothing is recording", err)

    def test_pause_waits_until_paused(self):
        ctl = self.control()
        ctl.claim({"uid": "u", "status": "recording", "final": "f"})
        t = self.recorder(ctl, lambda c, r: (self.assertEqual(r["kind"], "pause"),
                                             c.update_active(status="paused", segment=1, captured_s=75.0)))
        code, out, err = self.run_cli("pause", "--timeout", "10")
        t.join(25)
        self.assertEqual(code, 0, err)
        self.assertIn("paused after segment 1 (1:15 captured, saved)", out)
        ctl.release()

    def test_resume_waits_until_recording(self):
        ctl = self.control()
        ctl.claim({"uid": "u", "status": "paused", "final": "f"})
        t = self.recorder(ctl, lambda c, r: c.update_active(status="recording", segment=2))
        code, out, _ = self.run_cli("resume", "--timeout", "10")
        t.join(25)
        self.assertEqual(code, 0)
        self.assertIn("recording again: segment 2", out)
        ctl.release()

    def test_pause_that_does_not_happen_is_an_error(self):
        ctl = self.control()
        ctl.claim({"uid": "u", "status": "recording", "final": "f"})
        code, _, err = self.run_cli("pause", "--timeout", "0.3")
        self.assertEqual(code, 1)
        self.assertIn("still 'recording'", err)
        self.assertEqual(len(list((self.tmp / "home" / "state").glob("event-*.json"))), 1)
        code, out, _ = self.run_cli("pause", "--no-wait")
        self.assertEqual((code, out.strip()), (0, "pause requested"))
        ctl.release()

    def test_take_reports_the_count(self):
        ctl = self.control()
        ctl.claim({"uid": "u", "status": "recording", "final": "f", "takes": 0, "take_open": False})
        t = self.recorder(ctl, lambda c, r: c.update_active(takes=1, take_open=True))
        code, out, _ = self.run_cli("take")
        t.join(25)
        self.assertEqual(code, 0)
        self.assertIn("◉ take: 1 take(s), the last one open", out)
        t = self.recorder(ctl, lambda c, r: (self.assertEqual(r["kind"], "retake"),
                                             c.update_active(takes=1, take_open=True)))
        code, out, _ = self.run_cli("retake")
        t.join(25)
        self.assertIn("↺ retake: 1 take(s)", out)
        ctl.release()

    def test_rename_refuses_the_recording_in_progress(self):
        from tests.test_catalog import record
        uid = record(self.root, "inbox", "2026-10-05-paused")
        ctl = self.control()
        ctl.claim({"uid": uid, "status": "paused", "final": "f"})
        code, _, err = self.run_cli("rename", "last", "elsewhere")
        self.assertEqual(code, 1)
        self.assertIn("still recording (or paused)", err)
        ctl.release()
        self.assertEqual(self.run_cli("rename", "last", "elsewhere")[0], 0)

    def test_rename_says_when_a_counter_was_added(self):
        from tests.test_catalog import record
        record(self.root, "inbox", "2026-10-05-demo")
        record(self.root, "inbox", "2026-10-05-other")
        code, out, _ = self.run_cli("rename", "last", "demo")
        self.assertEqual(code, 0)
        self.assertIn("2026-10-05-demo-2.mp4  (that name was taken, so it got a counter)", out)


class ShimCommandsTest(DispatchTest):
    def test_new_commands_pass_through(self):
        cfg = dict(CFG, app_wsl=str(self.tmp))
        for argv in (["pause"], ["resume", "--no-wait"], ["take"], ["retake"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(shim, "load_shim_config", return_value=cfg), \
                    mock.patch.object(shim, "autosync"), \
                    mock.patch.object(shim.subprocess, "call", return_value=0) as call:
                self.assertEqual(self.run_main(*argv)[0], 0)
                self.assertEqual(call.call_args.args[0][4:], argv)
        self.assertIn("retake", shim.USAGE)


if __name__ == "__main__":
    unittest.main()
