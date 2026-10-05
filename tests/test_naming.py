"""Slug suggestion, kebab rules, stems and collision suffixing."""

import datetime as dt
import os
import unittest

from tests import TempDirMixin
from peep import naming as n

D = dt.date(2026, 10, 5)
WT = r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe\WindowsTerminal.exe"


class KebabTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(n.kebab("Hello, World!"), "hello-world")
        self.assertEqual(n.kebab("  --a__b--  "), "a-b")
        self.assertEqual(n.kebab("Café Résumé — Ünïcode"), "cafe-resume-unicode")
        self.assertEqual(n.kebab("!!!"), "")

    def test_truncates_at_word_boundary(self):
        s = n.kebab("one two three four five six seven eight nine ten eleven twelve")
        self.assertLessEqual(len(s), n.MAX_SLUG)
        self.assertEqual(s, "one-two-three-four-five-six-seven-eight-nine")
        self.assertEqual(len(n.kebab("a" * 80)), n.MAX_SLUG)

    def test_slug_from_user(self):
        self.assertEqual(n.slug_from_user("Bale Tutorial #2"), "bale-tutorial-2")
        with self.assertRaises(n.NamingError):
            n.slug_from_user("***")

    def test_validate_collection(self):
        self.assertEqual(n.validate_collection("Bale"), "bale")
        self.assertEqual(n.validate_collection("bale_tutorials-2"), "bale_tutorials-2")
        for bad in ("", "..", "a/b", "a\\b", "-x", "has space", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(n.NamingError):
                n.validate_collection(bad)


class SuggestTest(unittest.TestCase):
    def test_probe_case_terminal(self):
        # The exact foreground the session-A probe read on the laptop.
        self.assertEqual(n.suggest_slug(WT, "chordsphere@chordsphere: ~/peep-peep"), "terminal-peep-peep")

    def test_terminal_home_and_nested_paths(self):
        self.assertEqual(n.suggest_slug("WindowsTerminal.exe", "chordsphere@chordsphere: ~"), "terminal-home")
        self.assertEqual(n.suggest_slug("WindowsTerminal.exe", "me@box: /var/log/nginx/"), "terminal-nginx")

    def test_browser_suffix_dropped(self):
        self.assertEqual(n.suggest_slug("chrome.exe", "bale docs - Google Chrome"), "chrome-bale-docs")
        self.assertEqual(n.suggest_slug("msedge.exe", "Inbox | Outlook \u2014 Microsoft Edge"), "edge-inbox")

    def test_vscode_marks_and_segments(self):
        self.assertEqual(n.suggest_slug("Code.exe", "\u25cf cli.py - peep-peep - Visual Studio Code"),
                         "vscode-cli-py")

    def test_app_only_titles_and_fallbacks(self):
        self.assertEqual(n.suggest_slug("explorer.exe", "File Explorer"), "explorer")
        self.assertEqual(n.suggest_slug("obsidian.exe", "Obsidian"), "obsidian")
        self.assertEqual(n.suggest_slug(None, None), n.FALLBACK_SLUG)
        self.assertEqual(n.suggest_slug(None, "***"), n.FALLBACK_SLUG)
        self.assertEqual(n.suggest_slug("SomeTool.exe", ""), "sometool")

    def test_no_duplicate_prefix(self):
        self.assertEqual(n.suggest_slug("slack.exe", "Slack - general"), "slack-general")
        self.assertEqual(n.suggest_slug("notepad.exe", "notepad-notes.txt - Notepad"), "notepad-notes-txt")

    def test_length_bound(self):
        s = n.suggest_slug("chrome.exe", "A very long article title that goes on and on and on forever - Google Chrome")
        self.assertLessEqual(len(s), 40)
        self.assertTrue(s.startswith("chrome-a-very-long"))


class StemTest(TempDirMixin, unittest.TestCase):
    def test_stem_format(self):
        self.assertEqual(n.stem_for(D, "demo"), "2026-10-05-demo")
        self.assertEqual(n.stem_for(D, "demo", 3), "2026-10-05-demo-3")

    def test_allocate_counts_up_past_every_owned_suffix(self):
        d = str(self.tmp)
        self.assertEqual(n.allocate_stem(d, D, "demo"), "2026-10-05-demo")
        (self.tmp / "2026-10-05-demo.mp4").write_bytes(b"x")
        self.assertEqual(n.allocate_stem(d, D, "demo"), "2026-10-05-demo-2")
        (self.tmp / "2026-10-05-demo-2.recording.mkv").write_bytes(b"x")   # capture in progress
        self.assertEqual(n.allocate_stem(d, D, "demo"), "2026-10-05-demo-3")
        (self.tmp / "2026-10-05-demo-3.json").write_text("{}")            # orphaned sidecar
        self.assertEqual(n.allocate_stem(d, D, "demo"), "2026-10-05-demo-4")

    def test_allocate_with_injected_exists(self):
        taken = {os.path.join("X", "2026-10-05-a.mp4"), os.path.join("X", "2026-10-05-a-2.mkv")}
        self.assertEqual(n.allocate_stem("X", D, "a", exists=taken.__contains__), "2026-10-05-a-3")

    def test_allocate_limit(self):
        with self.assertRaises(n.NamingError):
            n.allocate_stem("X", D, "a", exists=lambda p: True, limit=3)

    def test_split_stem(self):
        self.assertEqual(n.split_stem("2026-10-05-demo"), ("2026-10-05", "demo", 1))
        self.assertEqual(n.split_stem("2026-10-05-demo-12"), ("2026-10-05", "demo", 12))
        with self.assertRaises(n.NamingError):
            n.split_stem("demo")


if __name__ == "__main__":
    unittest.main()
