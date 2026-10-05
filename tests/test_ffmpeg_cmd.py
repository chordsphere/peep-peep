"""ffmpeg argv assembly: the pure function from config to argv."""

import unittest

from peep import config as c
from peep import ffmpeg_cmd as f

MIC = "Microphone Array on SoundWire Device (6- Realtek XU)"


def spec(**kw):
    cfg = c.Config(root="C:/Users/chord/Videos/peep")
    return f.spec_from_config(cfg, ffmpeg="ffmpeg.exe", output="C:/v/x.recording.mkv", **kw)


def after(argv, flag):
    return argv[argv.index(flag) + 1]


class FilterGraphTest(unittest.TestCase):
    def test_qsv_is_the_probed_gpu_chain(self):
        # The exact chain the colour probe measured as correct (P2).
        self.assertEqual(f.filter_graph("qsv"),
                         "hwmap=derive_device=qsv,format=qsv,"
                         "vpp_qsv=format=nv12:out_color_matrix=bt709:out_range=tv")

    def test_qsv_scaled_matches_probe_p6(self):
        self.assertEqual(f.filter_graph("qsv", (1920, 1200)),
                         "hwmap=derive_device=qsv,format=qsv,"
                         "vpp_qsv=w=1920:h=1200:format=nv12:out_color_matrix=bt709:out_range=tv")

    def test_download_and_x264_convert_with_bt709(self):
        self.assertEqual(f.filter_graph("qsv-download"),
                         "hwdownload,format=bgra,scale=out_color_matrix=bt709:out_range=tv,format=nv12")
        self.assertEqual(f.filter_graph("x264", (1920, 1200)),
                         "hwdownload,format=bgra,scale=w=1920:h=1200:flags=lanczos:"
                         "out_color_matrix=bt709:out_range=tv,format=yuv420p")

    def test_unknown_pipeline(self):
        with self.assertRaises(ValueError):
            f.filter_graph("nvenc")


class CaptureArgvTest(unittest.TestCase):
    def test_default_capture(self):
        argv = f.build_capture_argv(spec())
        self.assertEqual(argv[0], "ffmpeg.exe")
        self.assertIn("-n", argv)                               # never overwrite
        self.assertEqual(after(argv, "-i"), "ddagrab=output_idx=0:framerate=30:draw_mouse=1")
        self.assertEqual(argv[argv.index("dshow") + 1:argv.index("dshow") + 5],
                         ["-audio_buffer_size", "50", "-i", f"audio={MIC}"])
        self.assertLess(argv.index("-thread_queue_size"), argv.index("dshow"))
        self.assertEqual(argv[argv.index("-map"):argv.index("-map") + 4], ["-map", "0:v:0", "-map", "1:a:0"])
        self.assertEqual(after(argv, "-c:v"), "h264_qsv")
        self.assertEqual(after(argv, "-preset"), "medium")
        self.assertEqual(after(argv, "-global_quality"), "25")
        self.assertEqual(after(argv, "-g"), "60")
        self.assertEqual(after(argv, "-fps_mode"), "cfr")
        self.assertEqual(after(argv, "-colorspace"), "bt709")
        self.assertEqual(after(argv, "-color_range"), "tv")
        self.assertEqual(after(argv, "-c:a"), "aac")
        self.assertEqual(after(argv, "-b:a"), "160k")
        self.assertEqual(argv[-3:], ["-f", "matroska", "C:/v/x.recording.mkv"])
        self.assertNotIn("-t", argv)
        self.assertNotIn("-itsoffset", argv)

    def test_input_options_precede_their_inputs(self):
        argv = f.build_capture_argv(spec())
        first_i = argv.index("-i")
        self.assertEqual(argv[first_i - 2:first_i], ["-f", "lavfi"])
        self.assertLess(argv.index("-vf"), argv.index("-c:v"))
        self.assertGreater(argv.index("-vf"), len(argv) - argv[::-1].index("-i") - 1)  # after the last input

    def test_no_audio(self):
        argv = f.build_capture_argv(spec(audio=False))
        self.assertNotIn("dshow", argv)
        self.assertNotIn("-map", argv)
        self.assertNotIn("-c:a", argv)
        self.assertEqual(argv.count("-i"), 1)

    def test_audio_offset(self):
        cfg = c.from_mapping({"audio": {"offset_ms": -120}})
        argv = f.build_capture_argv(f.spec_from_config(cfg, ffmpeg="ffmpeg", output="o.mkv"))
        self.assertEqual(after(argv, "-itsoffset"), "-0.120")
        self.assertLess(argv.index("-itsoffset"), argv.index("dshow"))

    def test_x264_fallback_and_overrides(self):
        argv = f.build_capture_argv(spec(pipeline="x264", fps=60, scale="1920x1200"))
        self.assertEqual(after(argv, "-c:v"), "libx264")
        self.assertEqual(after(argv, "-crf"), "20")
        self.assertEqual(after(argv, "-preset"), "veryfast")
        self.assertEqual(after(argv, "-g"), "120")
        self.assertIn("framerate=60", after(argv, "-i"))
        self.assertIn("w=1920:h=1200", after(argv, "-vf"))
        self.assertNotIn("-global_quality", argv)

    def test_native_scale_override_beats_config(self):
        cfg = c.from_mapping({"video": {"scale": "1920x1200"}})
        s = f.spec_from_config(cfg, ffmpeg="ffmpeg", output="o.mkv", scale="native")
        self.assertIsNone(s.scale)

    def test_doctor_style_null_capture(self):
        cfg = c.Config(root="C:/x")
        argv = f.build_capture_argv(f.spec_from_config(cfg, ffmpeg="ffmpeg", output="-",
                                                       output_format="null", duration_s=2))
        self.assertEqual(argv[-5:], ["-t", "2", "-f", "null", "-"])

    def test_draw_mouse_off(self):
        s = f.CaptureSpec(ffmpeg="ffmpeg", output="o.mkv", draw_mouse=False, output_idx=1)
        self.assertEqual(after(f.build_capture_argv(s), "-i"), "ddagrab=output_idx=1:framerate=30:draw_mouse=0")

    def test_encoder_names(self):
        self.assertEqual(f.ENCODERS, {"qsv": "h264_qsv", "qsv-download": "h264_qsv", "x264": "libx264"})


class OtherArgvTest(unittest.TestCase):
    def test_remux(self):
        self.assertEqual(f.build_remux_argv("ffmpeg", "a.recording.mkv", "a.mp4"),
                         ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-n", "-i",
                          "a.recording.mkv", "-map", "0", "-c", "copy", "-movflags", "+faststart", "a.mp4"])

    def test_list_devices(self):
        self.assertEqual(f.build_list_devices_argv("ffmpeg"),
                         ["ffmpeg", "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])


if __name__ == "__main__":
    unittest.main()
