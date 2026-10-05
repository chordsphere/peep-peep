"""Hotkey chords, the action table (conflict detection), and the listener's
registration/retry/dispatch logic against a fake Win32 message loop."""

import queue
import re
import threading
import unittest

from peep import config as c
from peep import hotkeys as hk
from peep.winapi import ForegroundInfo


class ChordTest(unittest.TestCase):
    def test_defaults_parse(self):
        self.assertEqual(hk.parse_chord("Ctrl+Alt+R"), hk.Chord(hk.MOD_CONTROL | hk.MOD_ALT, 0x52, "Ctrl+Alt+R"))
        self.assertEqual(hk.parse_chord("Ctrl+Alt+M").vk, 0x4D)
        self.assertEqual(hk.parse_chord("Ctrl+Alt+X").vk, 0x58)

    def test_spelling_is_normalised(self):
        self.assertEqual(hk.parse_chord(" alt + control + r ").text, "Ctrl+Alt+R")
        self.assertEqual(hk.parse_chord("win+shift+f9"), hk.Chord(hk.MOD_WIN | hk.MOD_SHIFT, 0x78, "Shift+Win+F9"))
        self.assertEqual(hk.parse_chord("Ctrl+Alt+pgdn").text, "Ctrl+Alt+PageDown")
        self.assertEqual(hk.parse_chord("Ctrl+Alt+7").vk, ord("7"))
        self.assertEqual(hk.parse_chord("Ctrl+Alt+F24").vk, 0x87)

    def test_bad_chords_say_why(self):
        for text, why in (("", "empty"), ("R", "Ctrl, Alt or Win"), ("Shift+R", "Ctrl, Alt or Win"),
                          ("Ctrl+Alt", "exactly one"), ("Ctrl+Alt+R+M", "exactly one"),
                          ("Ctrl+Ctrl+R", "repeated"), ("Ctrl++R", "malformed"),
                          ("Ctrl+Alt+F25", "unknown key"), ("Ctrl+Alt+Banana", "unknown key")):
            with self.subTest(text=text), self.assertRaisesRegex(hk.HotkeyError, why):
                hk.parse_chord(text)

    def test_table_and_conflicts(self):
        table = hk.table_from_agent_config(c.AgentConfig())
        self.assertEqual({a: ch.text for a, ch in table.items()},
                         {"record": "Ctrl+Alt+R", "mark": "Ctrl+Alt+M", "discard": "Ctrl+Alt+X"})
        with self.assertRaisesRegex(hk.HotkeyError, re.escape("mark_hotkey and record_hotkey are both Ctrl+Alt+R")):
            hk.build_table({"record": "Ctrl+Alt+R", "mark": "alt+ctrl+r"})
        with self.assertRaisesRegex(hk.HotkeyError, "unknown hotkey action"):
            hk.build_table({"rewind": "Ctrl+Alt+W"})
        with self.assertRaisesRegex(hk.HotkeyError, "discard_hotkey: .*unknown key"):
            hk.build_table({"discard": "Ctrl+Alt+Nope"})

    def test_config_validates_hotkeys(self):
        with self.assertRaisesRegex(c.ConfigError, r"\[agent\] mark_hotkey and record_hotkey"):
            c.from_mapping({"agent": {"mark_hotkey": "Ctrl+Alt+R"}})
        with self.assertRaisesRegex(c.ConfigError, "Ctrl, Alt or Win"):
            c.from_mapping({"agent": {"record_hotkey": "F9"}})
        cfg = c.from_mapping({"agent": {"record_hotkey": "Win+Shift+R"}})
        self.assertEqual(cfg.agent.record_hotkey, "Win+Shift+R")

    def test_describe_error_names_the_chord(self):
        ch = hk.parse_chord("Ctrl+Alt+R")
        self.assertEqual(hk.describe_error(ch, 1409), "Ctrl+Alt+R is already taken by another app (winerror 1409)")
        self.assertIn("winerror 5", hk.describe_error(ch, 5))


class FakeApi:
    """The listener's view of Win32: registration results scripted per chord
    text, and a message queue the test feeds."""

    def __init__(self, refuse=None):
        self.refuse = dict(refuse or {})       # chord text -> list of error codes, consumed per attempt
        self.registered, self.unregistered = {}, []
        self.messages = queue.Queue()
        self.chords = {}

    def thread_id(self):
        return 4242

    def register(self, ident, mods, vk):
        text = self.chords[(mods, vk)]
        errs = self.refuse.get(text)
        if errs:
            return errs.pop(0)
        self.registered[ident] = text
        return 0

    def unregister(self, ident):
        self.unregistered.append(ident)

    def wait(self, timeout_ms):
        try:
            return [self.messages.get(timeout=0.05)]
        except queue.Empty:
            return []

    def post_quit(self, thread_id):
        self.messages.put(("quit",))
        return True


class ListenerTest(unittest.TestCase):
    def make(self, refuse=None, retry_s=60, clock=None):
        table = hk.table_from_agent_config(c.AgentConfig())
        api = FakeApi(refuse)
        api.chords = {(ch.mods, ch.vk): ch.text for ch in table.values()}
        self.presses, self.problems, self.ok = [], [], []
        fg = ForegroundInfo("Bale docs - Google Chrome", r"C:\Program Files\Google\Chrome\chrome.exe")
        lst = hk.HotkeyListener(table, lambda a, f: self.presses.append((a, f)),
                                lambda a, ch, msg, final: self.problems.append((a, msg, final)),
                                lambda a, ch: self.ok.append(a), api_factory=lambda: api,
                                foreground=lambda: fg, retry_s=retry_s, retry_every_s=0,
                                clock=clock or (lambda: 0.0))
        return lst, api, fg

    def ident(self, lst, action):
        return next(i for i, a in lst.ids.items() if a == action)

    def test_registers_dispatches_and_unregisters(self):
        lst, api, fg = self.make()
        lst.start()
        self.assertEqual(sorted(api.registered.values()), ["Ctrl+Alt+M", "Ctrl+Alt+R", "Ctrl+Alt+X"])
        api.messages.put(("hotkey", self.ident(lst, "record")))
        api.messages.put(("hotkey", self.ident(lst, "mark")))
        api.messages.put(("hotkey", 0x1234))                 # not ours: logged, ignored
        lst.stop()
        self.assertEqual(self.presses, [("record", fg), ("mark", None)])   # foreground read for record only
        self.assertEqual(sorted(api.unregistered), sorted(api.registered))
        self.assertEqual(self.problems, [])
        self.assertTrue(all("registered" in s for s in lst.status().values()))

    def test_taken_chord_is_reported_then_retried_until_it_registers(self):
        with self.assertLogs("peep.hotkeys", "INFO") as logs:
            lst, api, _ = self.make(refuse={"Ctrl+Alt+R": [1409, 1409]})
            lst.start()
            for _ in range(200):
                if "Ctrl+Alt+R" in api.registered.values():
                    break
                threading.Event().wait(0.01)
            lst.stop()
        self.assertEqual(self.problems, [("record", "Ctrl+Alt+R is already taken by another app (winerror 1409)",
                                          False)])        # reported once, not on every retry
        self.assertIn("record", self.ok)
        self.assertTrue(any("hotkeys.register_failed" in l and "Ctrl+Alt+R" in l for l in logs.output))

    def test_chord_still_taken_after_the_window_is_final(self):
        t = {"now": 0.0}
        lst, api, _ = self.make(refuse={"Ctrl+Alt+X": [1409] * 100}, retry_s=1, clock=lambda: t["now"])
        lst.start()
        t["now"] = 5.0
        for _ in range(200):
            if any(final for _, _, final in self.problems):
                break
            threading.Event().wait(0.01)
        lst.stop()
        self.assertEqual([(a, final) for a, _, final in self.problems], [("discard", False), ("discard", True)])
        self.assertTrue(lst.status()["discard"].startswith("Ctrl+Alt+X FAILED: Ctrl+Alt+X is already taken"))
        self.assertNotIn(self.ident(lst, "discard"), api.unregistered)       # never registered, never released

    def test_no_retry_window_fails_at_once(self):
        lst, api, _ = self.make(refuse={"Ctrl+Alt+M": [5]}, retry_s=0)
        lst.start()
        lst.stop()
        self.assertEqual(self.problems, [("mark", "Ctrl+Alt+M could not be registered (winerror 5)", True)])


if __name__ == "__main__":
    unittest.main()
