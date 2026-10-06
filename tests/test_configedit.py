"""`peep config set/get/unset` (session A.1): comment-preserving line edits,
validated by the same loader the recorder uses, written atomically."""

import os
import tomllib
import unittest

from tests import TempDirMixin
from peep import config as c
from peep import configedit as ce

# The architect's config.toml as the A.1 probe read it (2026-10-06): session A's
# `peep config --init` template (written before B added [agent] and mark_color),
# with two keys uncommented by hand.
ARCHITECT = '# peep configuration. Every key is optional; anything left out keeps its\n# built-in default (shown here). Unknown keys are an error, on purpose.\n\n# root = "C:/Users/chord/Videos/peep"     # recordings land here, one folder per collection\n# default_collection = "inbox"\n# log_level = "INFO"                       # DEBUG also logs every ffmpeg stderr line\n\n[video]\n# fps = 30\n# pipeline = "qsv"                # qsv (GPU end to end) | qsv-download | x264\n# fallback = ["qsv-download", "x264"]\n# scale = ""                      # "" = native 2560x1600, or "1920x1200"\n# output_idx = 0\n# draw_mouse = true\n# qsv_preset = "medium"\n# qsv_global_quality = 25         # lower = better quality, bigger files\n# x264_preset = "veryfast"\n# x264_crf = 20\n# gop_seconds = 2\n\n[audio]\n# enabled = true\n# device = "Microphone Array on SoundWire Device (6- Realtek XU)"\n# bitrate = "160k"\n# buffer_ms = 50\noffset_ms = 500                   # positive delays the mic track; set from the sync smoke test\n\n[flash]\n# enabled = true\n# start_color = "#FF00FF"\n# stop_color = "#00FF00"\nduration_ms = 200\n# settle_ms = 400\n# lead_ms = 300\n\n[output]\n# container = "mp4"               # mp4 | mkv\n\n[ffmpeg]\n# path = "ffmpeg"\n# startup_timeout_s = 10.0\n# stop_timeout_s = 20.0\n'


def active(text):
    return [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


def comments(text):
    return [l for l in text.splitlines() if l.lstrip().startswith("#")]


class LineEditTest(unittest.TestCase):
    def test_set_replaces_an_active_value_and_keeps_its_comment_in_place(self):
        new, how = ce.set_in_text(ARCHITECT, ce.lookup("audio.offset_ms"), "-40")
        self.assertEqual(how, "replaced")
        line = next(l for l in new.splitlines() if l.startswith("offset_ms"))
        old = next(l for l in ARCHITECT.splitlines() if l.startswith("offset_ms"))
        self.assertEqual(line.index("#"), old.index("#"))
        self.assertTrue(line.startswith("offset_ms = -40 "))
        self.assertEqual(comments(new), comments(ARCHITECT))        # every comment survives
        self.assertEqual(len(new.splitlines()), len(ARCHITECT.splitlines()))

    def test_set_uncomments_the_template_line(self):
        # the friction that hid the offset bug: the value was typed into a commented line
        new, how = ce.set_in_text(ARCHITECT, ce.lookup("video.fps"), "60")
        self.assertEqual(how, "uncommented")
        self.assertIn("fps = 60", active(new))
        self.assertNotIn("# fps = 30", new)
        new, how = ce.set_in_text(ARCHITECT, ce.lookup("video.qsv_global_quality"), "22")
        line = next(l for l in new.splitlines() if l.startswith("qsv_global_quality"))
        old = next(l for l in ARCHITECT.splitlines() if "qsv_global_quality" in l)
        self.assertEqual(line.index("# lower"), old.index("# lower"))     # the comment keeps its column
        self.assertTrue(line.startswith("qsv_global_quality = 22 "))

    def test_set_adds_a_key_the_old_template_never_had(self):
        new, how = ce.set_in_text(ARCHITECT, ce.lookup("audio.sources"), '"both"')
        self.assertEqual(how, "added")
        lines = new.splitlines()
        self.assertEqual(lines[lines.index('sources = "both"') - 1].split()[0], "offset_ms")   # end of [audio]
        self.assertEqual(tomllib.loads(new)["audio"], {"offset_ms": 500, "sources": "both"})

    def test_set_creates_a_missing_section(self):
        new, how = ce.set_in_text(ARCHITECT, ce.lookup("agent.pill"), "false")
        self.assertEqual(how, "added-section")
        self.assertTrue(new.endswith("[agent]\npill = false\n"))

    def test_set_a_top_level_key_stays_above_the_first_section(self):
        new, how = ce.set_in_text(ARCHITECT, ce.lookup("root"), '"D:/rec"')
        self.assertEqual(how, "uncommented")
        data = tomllib.loads(new)
        self.assertEqual(data["root"], "D:/rec")
        new, how = ce.set_in_text("[video]\nfps = 60\n", ce.lookup("log_level"), '"DEBUG"')
        self.assertEqual(tomllib.loads(new), {"log_level": "DEBUG", "video": {"fps": 60}})

    def test_unset_restores_the_commented_default(self):
        new, how = ce.unset_in_text(ARCHITECT, ce.lookup("audio.offset_ms"))
        self.assertEqual(how, "restored-comment")
        self.assertNotIn("offset_ms", tomllib.loads(new)["audio"])
        self.assertIn(ce.template_line(ce.lookup("audio.offset_ms"), c.TEMPLATE), new.splitlines())

    def test_set_then_unset_round_trips_byte_for_byte(self):
        for key, value in (("video.fps", "60"), ("audio.mix", '"separate"'), ("flash.lead_ms", "250")):
            new, _ = ce.set_in_text(c.TEMPLATE, ce.lookup(key), value)
            back, how = ce.unset_in_text(new, ce.lookup(key))
            self.assertEqual(back, c.TEMPLATE, key)

    def test_unset_a_key_that_is_not_set(self):
        self.assertEqual(ce.unset_in_text(ARCHITECT, ce.lookup("video.fps")), (ARCHITECT, "not-set"))

    def test_unset_without_a_template_line_removes(self):
        new, how = ce.unset_in_text("[audio]\noffset_ms = 5\n", ce.lookup("audio.offset_ms"), template="")
        self.assertEqual((new, how), ("[audio]\n", "removed"))

    def test_duplicates_and_multiline_values_are_refused(self):
        with self.assertRaisesRegex(c.ConfigError, "set 2 times"):
            ce.set_in_text("[video]\nfps = 1\nfps = 2\n", ce.lookup("video.fps"), "3")
        with self.assertRaisesRegex(c.ConfigError, "multi-line arrays"):
            ce.set_in_text('[video]\nfallback = [\n  "x264",\n]\n', ce.lookup("video.fallback"), '["x264"]')

    def test_value_comment_split_respects_strings(self):
        self.assertEqual(ce.split_value_comment('"a # b"   # note'), ('"a # b"', "# note"))
        self.assertEqual(ce.split_value_comment("'C:\\x#y' # p"), ("'C:\\x#y'", "# p"))
        self.assertEqual(ce.split_value_comment('["a", "b"]  # list'), ('["a", "b"]', "# list"))
        self.assertEqual(ce.split_value_comment("42"), ("42", ""))


class ValuesTest(unittest.TestCase):
    def test_cli_values_are_typed_by_the_key(self):
        p = ce.parse_cli_value
        self.assertEqual(p(ce.lookup("audio.offset_ms"), "-120"), -120)
        self.assertEqual(p(ce.lookup("audio.mic_gain"), "1.5"), 1.5)
        self.assertEqual(p(ce.lookup("audio.clap"), "off"), False)
        self.assertEqual(p(ce.lookup("agent.pill"), "YES"), True)
        self.assertEqual(p(ce.lookup("audio.device"), "Mic (6- Realtek)"), "Mic (6- Realtek)")
        self.assertEqual(p(ce.lookup("video.fallback"), "x264, qsv-download"), ["x264", "qsv-download"])
        self.assertEqual(p(ce.lookup("video.fallback"), '["x264"]'), ["x264"])
        self.assertEqual(p(ce.lookup("video.fallback"), ""), [])
        for key, bad in (("audio.offset_ms", "half"), ("audio.clap", "maybe"), ("ffmpeg.stop_timeout_s", "x")):
            with self.assertRaises(c.ConfigError):
                p(ce.lookup(key), bad)

    def test_toml_literals(self):
        self.assertEqual(ce.toml_literal(True), "true")
        self.assertEqual(ce.toml_literal(2.0), "2.0")
        self.assertEqual(ce.toml_literal(1e-05), "1e-05")
        self.assertEqual(ce.toml_literal('C:\\Users\\"x"'), '"C:\\\\Users\\\\\\"x\\""')
        for v in ("tab\there", "ctl\x01", "ünïcode", 'q"'):
            self.assertEqual(tomllib.loads(f"v = {ce.toml_literal(v)}")["v"], v)

    def test_unknown_key_suggests(self):
        with self.assertRaisesRegex(c.ConfigError, "did you mean audio.offset_ms"):
            ce.lookup("audio.ofset_ms")

    def test_every_loader_key_is_settable(self):
        keys = ce.known_keys()
        self.assertIn("audio.sources", keys)
        self.assertIn("agent.record_hotkey", keys)
        self.assertIn("root", keys)
        self.assertEqual(len(keys), 3 + sum(len(c._field_types(cls)) for cls in c._SECTIONS.values()))


class FileTest(TempDirMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.path = self.tmp / "config.toml"
        self.env = {"USERPROFILE": "C:\\Users\\chord"}

    def test_set_get_unset_on_the_architects_file(self):
        self.path.write_text(ARCHITECT, encoding="utf-8")
        res = ce.set_value(self.path, "audio.offset_ms", "0", self.env)
        self.assertEqual((res.old, res.new, res.how), (500, 0, "replaced"))
        res = ce.unset_value(self.path, "audio.offset_ms", self.env)
        self.assertEqual((res.old, res.new, res.how), (0, 0, "restored-comment"))
        res = ce.set_value(self.path, "audio.sources", "both", self.env)
        self.assertEqual((res.old, res.new), ("system", "both"))
        cfg = c.load(self.path, self.env)                       # the recorder's loader agrees
        self.assertEqual((cfg.audio.sources, cfg.audio.offset_ms, cfg.flash.duration_ms), ("both", 0, 200))
        text = self.path.read_text(encoding="utf-8")
        self.assertTrue(set(comments(ARCHITECT)) <= set(comments(text)))
        self.assertFalse((self.tmp / "config.toml.tmp").exists())

    def test_invalid_values_never_touch_the_file(self):
        self.path.write_text(ARCHITECT, encoding="utf-8")
        before = self.path.read_bytes()
        for key, value, why in (("audio.sources", "speakers", "audio.sources must be one of"),
                                ("flash.duration_ms", "5", "20..2000"),
                                ("agent.record_hotkey", "R", "agent"),
                                ("video.scale", "1921x1200", "even")):
            with self.assertRaisesRegex(c.ConfigError, why):
                ce.set_value(self.path, key, value, self.env)
            self.assertEqual(self.path.read_bytes(), before, key)

    def test_set_without_a_file_writes_the_template_first(self):
        res = ce.set_value(self.path, "audio.sources", "mic", self.env)
        self.assertTrue(res.created_file)
        text = self.path.read_text(encoding="utf-8")
        self.assertIn('sources = "mic"', text)
        self.assertEqual(comments(text), [l for l in comments(c.TEMPLATE) if not l.startswith("# sources")])

    def test_a_broken_file_can_be_repaired_by_set(self):
        self.path.write_text("[audio]\nsources = \"speakers\"\n", encoding="utf-8")
        with self.assertRaises(c.ConfigError):
            c.load(self.path, self.env)
        res = ce.set_value(self.path, "audio.sources", "system", self.env)
        self.assertEqual((res.old, res.new), (None, "system"))
        self.assertEqual(c.load(self.path, self.env).audio.sources, "system")

    def test_get_rows(self):
        self.path.write_text(ARCHITECT, encoding="utf-8")
        cfg = c.load(self.path, self.env)
        self.assertEqual(ce.get_rows(cfg, self.path, "audio.offset_ms"), [("audio.offset_ms", "500", "file")])
        rows = {k: (v, src) for k, v, src in ce.get_rows(cfg, self.path)}
        self.assertEqual(rows["audio.sources"], ('"system"', "default"))
        self.assertEqual(rows["flash.duration_ms"], ("200", "file"))
        self.assertEqual(rows["video.fallback"], ('["qsv-download", "x264"]', "default"))

    def test_the_write_changes_mtime_for_the_agents_reload(self):
        self.path.write_text(ARCHITECT, encoding="utf-8")
        os.utime(self.path, (1, 1))
        ce.set_value(self.path, "flash.duration_ms", "250", self.env)
        self.assertGreater(self.path.stat().st_mtime, 1)


if __name__ == "__main__":
    unittest.main()
