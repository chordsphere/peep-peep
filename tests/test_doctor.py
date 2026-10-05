"""`peep doctor` against the ffmpeg listings the session-A probe captured
on the laptop (2026-10-05, ffmpeg 9.0.2 Gyan full build)."""

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


LAPTOP = {"-version": (0, VERSION, ""), "-filters": (0, FILTERS, ""), "-devices": (0, DEVICES, ""),
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
                  "fallback x264", "microphone", "storage root", "flash (tkinter)", "capture test (qsv + mic)"):
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
        cfg = c.from_mapping({"root": str(self.tmp), "audio": {"device": "Headset Mic"}})
        mic = self.by_name(self.doctor(cfg=cfg).checks())["microphone"]
        self.assertFalse(mic.ok)
        self.assertIn("Microphone Array on SoundWire Device (6- Realtek XU)", mic.detail)
        self.assertIn(ALT, mic.detail)

    def test_mic_by_alternative_name_and_disabled(self):
        cfg = c.from_mapping({"root": str(self.tmp), "audio": {"device": ALT}})
        self.assertTrue(self.by_name(self.doctor(cfg=cfg).checks())["microphone"].ok)
        cfg = c.from_mapping({"root": str(self.tmp), "audio": {"enabled": False}})
        self.assertTrue(self.by_name(self.doctor(cfg=cfg).checks())["microphone"].ok)

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
