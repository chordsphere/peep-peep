"""`peep doctor` against the ffmpeg listings the session-A probe captured
on the laptop (2026-10-05, ffmpeg 9.0.2 Gyan full build)."""

import json
import unittest

from tests import TempDirMixin
from peep import config as c
from peep.doctor import Doctor, WINGET_FFMPEG, WINGET_PYTHON, parse_dshow_audio, render

FF = r"C:\Users\chord\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin\ffmpeg.EXE"

VERSION = "ffmpeg version 9.0.2-full_build-www.gyan.dev Copyright (c) 2000-2026 the FFmpeg developers\n"
DEVICES = """Devices:
 D. = Demuxing supported
 .E = Muxing supported
 ---
  E caca            caca (color ASCII art) output device
 D  dshow           DirectShow capture
 D  gdigrab         GDI API Windows frame grabber
 D  lavfi           Libavfilter virtual input device
"""
ENCODERS = """ V..... h264_qsv             H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (Intel Quick Sync Video acceleration) (codec h264)
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D libx264rgb           libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 RGB (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
"""
FILTERS = """ .. hwdownload        V->V       Download a hardware frame to a normal frame
 .. hwmap             V->V       Map hardware frames
 .. scale_qsv         V->V       Quick Sync Video "scaling and format conversion"
 .. vpp_qsv           V->V       Quick Sync Video "VPP"
 .. ddagrab           |->V       Grab Windows Desktop images using Desktop Duplication API
"""
LIST_DEVICES = r"""[in#0 @ 00000247e48601c0] "Integrated Webcam" (video)
[in#0 @ 00000247e48601c0]   Alternative name "@device_pnp_\\?\usb#vid_0bda&pid_5591&mi_00#6&14bc9e39&0&0000#{65e8773d-8f56-11d0-a3b9-00a0c9223196}\global"
[in#0 @ 00000247e48601c0] "Microphone Array on SoundWire Device (6- Realtek XU)" (audio)
[in#0 @ 00000247e48601c0]   Alternative name "@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{85A9C569-9688-43B6-833E-478DF7BBFDDC}"
Error opening input file dummy.
"""
ALT = r"@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{85A9C569-9688-43B6-833E-478DF7BBFDDC}"


def fake_run(table):
    def run(argv, timeout):
        key = " ".join(argv[1:])
        for needle, result in table.items():
            if needle in key:
                return result
        return 1, "", f"unexpected {key}"
    return run


# `python -m peep.wasapi list` / `test` output, from the A.1 probe (2026-10-06)
FX = {"id": "{0.0.0.00000000}.{a6935c5e-eeda-431a-a624-864c87bcb4bd}",
      "name": "FxSound Speakers (FxSound Audio Enhancer)", "flow": "render", "state": "active", "is_default": True}
SPK = {"id": "{0.0.0.00000000}.{63e24de0-203c-403d-8b61-82ed6b8a217c}", "name": "Speakers (Realtek XU)",
       "flow": "render", "state": "active", "is_default": False}
MICEP = {"id": "{0.0.1.00000000}.{85a9c569-9688-43b6-833e-478df7bbfddc}",
         "name": "Microphone Array on SoundWire Device (6- Realtek XU)", "flow": "capture", "state": "active",
         "is_default": True}
F32 = {"rate": 48000, "channels": 2, "sample_bits": 32, "is_float": True, "valid_bits": 0, "channel_mask": 3,
       "block_align": 8, "ffmpeg_format": "f32le"}
WASAPI_LIST = json.dumps({"render": [FX, SPK, dict(SPK, name="Speakers (2- Realtek XU)", state="not present",
                                                    id="x")],
                          "capture": [MICEP], "default_render": FX, "default_capture": MICEP,
                          "mix_formats": {FX["name"]: F32, SPK["name"]: F32, MICEP["name"]: F32}})
WASAPI_TEST = json.dumps({"ok": True, "source": "system", "endpoint": FX, "format": F32, "peak": 0.5,
                          "non_silent": True, "tone_latency_ms": 69.5, "packets_before_tone": 0})

LAPTOP = {"SystemExit(main()) list": (0, WASAPI_LIST + "\n", ""),
          "SystemExit(main()) test": (0, WASAPI_TEST + "\n", ""),
          "-version": (0, VERSION, ""), "-filters": (0, FILTERS, ""), "-devices": (0, DEVICES, ""),
          "-encoders": (0, ENCODERS, ""), "-list_devices": (1, "", LIST_DEVICES),
          "ddagrab": (0, "", "frame=   60 fps= 30 time=00:00:02.00\n")}


class ParseTest(unittest.TestCase):
    def test_parse_probe_listing(self):
        self.assertEqual(parse_dshow_audio(LIST_DEVICES),
                         [("Microphone Array on SoundWire Device (6- Realtek XU)", ALT)])

    def test_parse_without_alternative_names(self):
        text = '"Mic A" (audio)\n"Mic B" (audio)\n"Cam" (video)\n'
        self.assertEqual(parse_dshow_audio(text), [("Mic A", None), ("Mic B", None)])


class DoctorTest(TempDirMixin, unittest.TestCase):
    def doctor(self, table=None, which=lambda name: FF, cfg=None, py=(3, 12, 10), tk_ok=True):
        def tk():
            if not tk_ok:
                raise ImportError("No module named 'tkinter'")
            return type("tk", (), {"TkVersion": 8.6})
        cfg = cfg or c.from_mapping({"root": str(self.tmp / "Videos" / "peep")})
        return Doctor(cfg, run=fake_run(table or LAPTOP), which=which,
                      environ={"PEEP_HOME": str(self.tmp / "home")}, python_version=py, tk_import=tk)

    def by_name(self, checks):
        return {ch.name: ch for ch in checks}

    def test_laptop_is_all_green(self):
        checks = self.doctor().checks(capture_test=True)
        text, code = render(checks)
        self.assertEqual(code, 0, text)
        names = self.by_name(checks)
        for n in ("python", "ffmpeg", "input ddagrab", "input dshow", "pipeline qsv", "fallback qsv-download",
                  "fallback x264", "audio sources", "system audio", "loopback capture", "storage root",
                  "flash (tkinter)", "capture test (qsv, video only)"):
            self.assertTrue(names[n].ok, f"{n}: {names[n].detail}")
        self.assertIn("9.0.2", names["ffmpeg"].detail)

    def test_missing_ffmpeg_prints_winget_line(self):
        checks = self.doctor(which=lambda name: None).checks()
        text, code = render(checks)
        self.assertEqual(code, 1)
        self.assertIn(WINGET_FFMPEG, text)
        self.assertNotIn("microphone", self.by_name(checks))   # cannot check devices without ffmpeg

    def test_old_python_prints_winget_line(self):
        text, code = render(self.doctor(py=(3, 10, 0)).checks())
        self.assertEqual(code, 1)
        self.assertIn(WINGET_PYTHON, text)

    def test_mic_missing_lists_what_exists(self):
        cfg = c.from_mapping({"root": str(self.tmp), "audio": {"device": "Headset Mic", "sources": "mic",
                                                                "mic_backend": "dshow"}})
        mic = self.by_name(self.doctor(cfg=cfg).checks())["microphone (dshow)"]
        self.assertFalse(mic.ok)
        self.assertIn("Microphone Array on SoundWire Device (6- Realtek XU)", mic.detail)
        self.assertIn(ALT, mic.detail)

    def test_mic_by_alternative_name_and_disabled(self):
        cfg = c.from_mapping({"root": str(self.tmp), "audio": {"device": ALT, "sources": "mic",
                                                                "mic_backend": "dshow"}})
        self.assertTrue(self.by_name(self.doctor(cfg=cfg).checks())["microphone (dshow)"].ok)
        cfg = c.from_mapping({"root": str(self.tmp), "audio": {"enabled": False}})
        checks = self.by_name(self.doctor(cfg=cfg).checks())
        self.assertIn("none", checks["audio sources"].detail)
        self.assertNotIn("loopback capture", checks)

    def test_essentials_build_without_qsv(self):
        table = dict(LAPTOP)
        table["-encoders"] = (0, " V....D libx264              libx264 H.264\n", "")
        table["-filters"] = (0, " .. hwdownload        V->V       Download\n .. ddagrab |->V Grab\n", "")
        checks = self.by_name(self.doctor(table).checks())
        self.assertFalse(checks["pipeline qsv"].ok)
        self.assertIn("h264_qsv", checks["pipeline qsv"].detail)
        self.assertTrue(checks["fallback x264"].ok)
        self.assertTrue(checks["fallback qsv-download"].warn_only)

    def test_tk_missing_is_a_warning_only(self):
        checks = self.doctor(tk_ok=False).checks()
        self.assertEqual(render(checks)[1], 0)
        self.assertTrue(self.by_name(checks)["flash (tkinter)"].warn_only)

    def test_failed_capture_test_reports_tail(self):
        table = dict(LAPTOP)
        table["ddagrab"] = (1, "", "[h264_qsv @ 0] Error initializing an internal MFX session\nConversion failed!\n")
        ch = self.doctor(table).checks(capture_test=True)[-1]
        self.assertFalse(ch.ok)
        self.assertIn("Conversion failed", ch.detail)


if __name__ == "__main__":
    unittest.main()


class AudioDoctorTest(TempDirMixin, unittest.TestCase):
    """A.1's audio checks through the WASAPI child (stubbed runner)."""

    def checks(self, audio=None, table=None):
        cfg = c.from_mapping({"root": str(self.tmp / "Videos" / "peep"), "audio": audio or {}})
        d = Doctor(cfg, run=fake_run(table or LAPTOP), which=lambda n: FF, environ={"PEEP_HOME": str(self.tmp)},
                   tk_import=lambda: type("tk", (), {"TkVersion": 8.6}), python="python.exe")
        return {ch.name: ch for ch in d.checks()}

    def test_default_system_audio(self):
        ch = self.checks()
        self.assertIn("FxSound Speakers (FxSound Audio Enhancer)", ch["system audio"].detail)
        self.assertIn("Windows default; 48000 Hz, 2 ch, f32le", ch["system audio"].detail)
        self.assertTrue(ch["loopback capture"].ok)
        self.assertIn("heard (peak 0.5, 69.5 ms", ch["loopback capture"].detail)
        self.assertTrue((self.tmp / "clap.wav").exists())          # the test tone it played
        self.assertNotIn("microphone", ch)
        self.assertNotIn("audio offset", ch)

    def test_the_child_is_bootstrapped_without_pythonpath(self):
        d = Doctor(c.Config(), python="python.exe")
        argv = d.wasapi_argv("list")
        self.assertEqual(argv[:4], ["python.exe", "-X", "utf8", "-c"])
        self.assertIn("from peep.wasapi import main", argv[4])
        self.assertEqual(argv[-1], "list")

    def test_configured_devices_resolve_or_list_the_choices(self):
        ch = self.checks({"sources": "both", "system_device": "Realtek", "device": "Headset"})
        self.assertTrue(ch["system audio"].ok)
        self.assertIn("'Speakers (Realtek XU)' (configured", ch["system audio"].detail)
        self.assertFalse(ch["microphone"].ok)
        self.assertIn("Microphone Array on SoundWire Device (6- Realtek XU)", ch["microphone"].detail)
        self.assertIn("peep config set audio.device", ch["microphone"].fix)

    def test_silence_during_the_tone_is_a_warning(self):
        table = dict(LAPTOP)
        table["SystemExit(main()) test"] = (0, json.dumps({"ok": True, "endpoint": FX, "non_silent": False,
                                                           "peak": 0.0, "tone_latency_ms": None}), "")
        ch = self.checks(table=table)
        self.assertFalse(ch["loopback capture"].ok)
        self.assertTrue(ch["loopback capture"].warn_only)
        self.assertIn("NOT heard", ch["loopback capture"].detail)

    def test_a_crashed_child_is_named(self):
        table = dict(LAPTOP)
        table["SystemExit(main()) list"] = (3221225477, "", "")
        ch = self.checks(table=table)
        self.assertFalse(ch["audio devices"].ok)
        self.assertIn("access violation", ch["audio devices"].detail)

    def test_leftover_offset_is_flagged_with_the_fix(self):
        ch = self.checks({"offset_ms": 500})
        self.assertFalse(ch["audio offset"].ok)
        self.assertTrue(ch["audio offset"].warn_only)
        self.assertEqual(ch["audio offset"].fix.split("   ")[0], "peep config unset audio.offset_ms")

    def test_dshow_backend_uses_the_dshow_listing(self):
        ch = self.checks({"sources": "mic", "mic_backend": "dshow"})
        self.assertTrue(ch["microphone (dshow)"].ok)
        self.assertNotIn("loopback capture", ch)
