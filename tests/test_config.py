"""Config defaults (must work on the laptop unchanged) and overrides."""

import tomllib
import unittest

from tests import TempDirMixin
from peep import config as c


class DefaultsTest(unittest.TestCase):
    def test_defaults_match_the_laptop(self):
        cfg = c.load(environ={"PEEP_HOME": "/nonexistent-peep-home", "USERPROFILE": "C:\\Users\\chord"})
        self.assertEqual(cfg.root, "C:/Users/chord/Videos/peep")
        self.assertEqual(cfg.source, "defaults")
        self.assertEqual(cfg.default_collection, "inbox")
        self.assertEqual(cfg.video.fps, 30)
        self.assertEqual(cfg.video.pipeline, "qsv")
        self.assertEqual(cfg.video.fallback, ("qsv-download", "x264"))
        self.assertEqual(cfg.audio.device, "")                  # A.1: the Windows default recording device
        self.assertEqual(cfg.audio.sources, "system")           # A.1: computer audio by default (decision 5)
        self.assertEqual(cfg.audio.mic_backend, "wasapi")
        self.assertEqual(cfg.audio.epoch_lead_ms, 58)           # probe-measured, see the README
        self.assertEqual(cfg.flash.start_color, "#FF00FF")
        self.assertEqual(cfg.flash.stop_color, "#00FF00")
        self.assertEqual(cfg.flash.duration_ms, 200)       # raised from 150 after the 2026-10-04 smoke test
        self.assertEqual(cfg.flash.mark_color, "#00FFFF")
        self.assertEqual((cfg.agent.record_hotkey, cfg.agent.mark_hotkey, cfg.agent.discard_hotkey),
                         ("Ctrl+Alt+R", "Ctrl+Alt+M", "Ctrl+Alt+X"))
        self.assertTrue(cfg.agent.pill)
        self.assertEqual(cfg.output.container, "mp4")

    def test_root_falls_back_to_literal_without_userprofile(self):
        self.assertEqual(c.default_root({}), "C:/Users/chord/Videos/peep")

    def test_template_is_valid_toml_and_all_commented(self):
        data = tomllib.loads(c.TEMPLATE)
        self.assertEqual(set(data), {"video", "audio", "flash", "output", "ffmpeg", "agent", "render",
                                     "auto_takes"})               # C1b: render; C1d: auto_takes
        self.assertTrue(all(v == {} for v in data.values()))
        self.assertEqual(c.from_mapping(data).video, c.Config().video)


class OverrideTest(TempDirMixin, unittest.TestCase):
    def write(self, text):
        p = self.tmp / "config.toml"
        p.write_text(text, encoding="utf-8")
        return p

    def test_file_overrides_selected_keys_only(self):
        p = self.write('root = "D:/rec"\n[video]\nfps = 60\nscale = "1920x1200"\nfallback = ["x264"]\n'
                       '[flash]\nduration_ms = 200\n[ffmpeg]\nstop_timeout_s = 5\n')
        cfg = c.load(p, environ={})
        self.assertEqual(cfg.root, "D:/rec")
        self.assertEqual(cfg.video.fps, 60)
        self.assertEqual(cfg.video.scale, "1920x1200")
        self.assertEqual(cfg.video.fallback, ("x264",))
        self.assertEqual(cfg.video.pipeline, "qsv")          # untouched default
        self.assertEqual(cfg.flash.duration_ms, 200)
        self.assertEqual(cfg.flash.start_color, "#FF00FF")   # untouched default
        self.assertEqual(cfg.ffmpeg.stop_timeout_s, 5.0)     # int accepted for float
        self.assertEqual(cfg.source, str(p))

    def test_config_path_from_environment(self):
        p = self.write("[audio]\nenabled = false\n")
        cfg = c.load(environ={"PEEP_CONFIG": str(p)})
        self.assertFalse(cfg.audio.enabled)

    def test_unknown_keys_are_errors(self):
        with self.assertRaisesRegex(c.ConfigError, "unknown key.*fsp"):
            c.load(self.write("[video]\nfsp = 30\n"), environ={})
        with self.assertRaisesRegex(c.ConfigError, "unknown top-level"):
            c.load(self.write("rooot = 'x'\n"), environ={})

    def test_wrong_types_are_errors(self):
        for text, pattern in [("[video]\nfps = '30'\n", "video.fps must be int"),
                              ("[video]\nfps = true\n", "video.fps must be an integer"),
                              ("[audio]\nenabled = 1\n", "audio.enabled must be bool"),
                              ("[video]\nfallback = 'x264'\n", "list of strings")]:
            with self.subTest(text=text), self.assertRaisesRegex(c.ConfigError, pattern):
                c.load(self.write(text), environ={})

    def test_invalid_values_are_errors(self):
        for text, pattern in [("[video]\npipeline = 'nvenc'\n", "video.pipeline"),
                              ("[video]\nscale = '1920by1200'\n", "scale must be"),
                              ("[video]\nscale = '1921x1200'\n", "even"),
                              ("[flash]\nstart_color = 'magenta'\n", "#RRGGBB"),
                              ("[output]\ncontainer = 'avi'\n", "output.container"),
                              ("[audio]\ndevice = ' '\n", "audio.device is empty"),
                              ("root = '//wsl.localhost/Ubuntu/home/x'\n", "never under"),
                              ('root = "\\\\\\\\wsl$\\\\Ubuntu\\\\x"\n', "never under"),
                              ("root = '\\\\wsl.localhost\\Ubuntu\\home'\n", "never under")]:
            with self.subTest(text=text), self.assertRaisesRegex(c.ConfigError, pattern):
                c.load(self.write(text), environ={})

    def test_bad_toml_reports_path(self):
        p = self.write("[video\n")
        with self.assertRaisesRegex(c.ConfigError, "invalid TOML"):
            c.load(p, environ={})

    def test_parse_scale(self):
        self.assertIsNone(c.parse_scale(""))
        self.assertIsNone(c.parse_scale("native"))
        self.assertEqual(c.parse_scale("1920x1200"), (1920, 1200))



class AgentConfigTest(unittest.TestCase):
    """Session B's [agent] section and the mark colour."""

    def test_overrides(self):
        cfg = c.from_mapping({"agent": {"record_hotkey": "Ctrl+Shift+F9", "pill": False, "pill_position": "bottom-left",
                                        "dialog": False, "hotkey_retry_s": 0}, "flash": {"mark_color": "#FF8000"}})
        self.assertEqual((cfg.agent.record_hotkey, cfg.agent.pill, cfg.agent.pill_position, cfg.agent.dialog),
                         ("Ctrl+Shift+F9", False, "bottom-left", False))
        self.assertEqual(cfg.flash.mark_color, "#FF8000")   # (C1a: yellow is the retake, now correct, patch)

    def test_invalid_agent_values(self):
        for data, why in (({"agent": {"pill_position": "middle"}}, "pill_position"),
                          ({"agent": {"pill_margin_px": -1}}, "pill_margin_px"),
                          ({"agent": {"hotkey_retry_s": 99999}}, "hotkey_retry_s"),
                          ({"agent": {"pill": "yes"}}, "agent.pill must be bool"),
                          ({"agent": {"tray": True}}, r"unknown key\(s\) in \[agent\]"),
                          ({"agent": {"discard_hotkey": "Ctrl+Alt+M"}}, "discard_hotkey and mark_hotkey"),
                          ({"flash": {"mark_color": "cyan"}}, "mark_color must be #RRGGBB"),
                          ({"flash": {"mark_color": "#ff00ff"}}, "too alike")):
            with self.subTest(data=data), self.assertRaisesRegex(c.ConfigError, why):
                c.from_mapping(data)

    def test_template_documents_every_agent_key(self):
        for f in c.AgentConfig.__dataclass_fields__:
            self.assertIn(f"# {f} = ", c.TEMPLATE)
        self.assertIn("# mark_color = ", c.TEMPLATE)

if __name__ == "__main__":
    unittest.main()
