"""ffmpeg argv assembly: the pure function from config to argv."""

import unittest

from peep import config as c
from peep import ffmpeg_cmd as f
from peep.wasapi import CaptureReady, PcmFormat

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
        argv = f.build_capture_argv(spec(audio_inputs=inputs(c.Config(root="C:/x"), ("system",))))
        self.assertEqual(argv[0], "ffmpeg.exe")
        self.assertIn("-n", argv)                               # never overwrite
        self.assertEqual(after(argv, "-i"), "ddagrab=output_idx=0:framerate=30:draw_mouse=1")
        self.assertEqual(argv[argv.index("-thread_queue_size"):argv.index("-map")],
                         ["-thread_queue_size", "1024", "-rw_timeout", "3000000", "-f", "f32le", "-ar", "48000",
                          "-ac", "2", "-i", "tcp://127.0.0.1:50001"])
        self.assertNotIn("dshow", argv)
        self.assertNotIn("-filter_complex", argv)
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
        argv = f.build_capture_argv(spec())
        self.assertNotIn("dshow", argv)
        self.assertNotIn("-map", argv)
        self.assertNotIn("-c:a", argv)
        self.assertEqual(argv.count("-i"), 1)

    def test_itsoffset_is_gone(self):
        # A.1: -itsoffset reached the .mp4 only as an edit list (probe: sync5's elst [(477, -1), ...]),
        # which a player may ignore. Offsets are now applied to the samples or by the anchor.
        for src in (("system",), ("mic",), ("system", "mic")):
            for backend in ("wasapi", "dshow"):
                cfg = c.from_mapping({"audio": {"offset_ms": -120, "mic_backend": backend}})
                argv = f.build_capture_argv(spec(audio_inputs=inputs(cfg, src)))
                self.assertNotIn("-itsoffset", argv)

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


READY = {"system": CaptureReady(50001, PcmFormat(48000, 2, 32, True), {"name": "FxSound Speakers"}, 0.0),
         "mic": CaptureReady(50002, PcmFormat(48000, 2, 32, True), {"name": "Microphone Array"}, 0.0)}


def inputs(cfg, sources, ready=READY):
    return f.audio_inputs_for(cfg, sources, ready)


class AudioArgvTest(unittest.TestCase):
    """Pipeline assembly for every audio.sources value (A.1)."""

    def argv(self, sources, **audio):
        cfg = c.from_mapping({"audio": audio} if audio else {})
        return f.build_capture_argv(f.spec_from_config(cfg, ffmpeg="ffmpeg", output="o.mkv",
                                                       audio_inputs=inputs(cfg, sources)))

    def test_none(self):
        argv = self.argv(c.source_set("none"))
        self.assertEqual(argv.count("-i"), 1)
        self.assertNotIn("-c:a", argv)
        self.assertNotIn("-map", argv)

    def test_system_only_maps_straight_through(self):
        argv = self.argv(c.source_set("system"))
        self.assertEqual(argv.count("-i"), 2)
        self.assertIn("tcp://127.0.0.1:50001", argv)
        self.assertNotIn("-filter_complex", argv)
        self.assertEqual(after(argv, "-c:a"), "aac")

    def test_mic_only_is_wasapi_by_default(self):
        argv = self.argv(c.source_set("mic"))
        self.assertIn("tcp://127.0.0.1:50002", argv)
        self.assertNotIn("dshow", argv)

    def test_both_mixes_into_one_track_system_first(self):
        argv = self.argv(c.source_set("both"))
        self.assertLess(argv.index("tcp://127.0.0.1:50001"), argv.index("tcp://127.0.0.1:50002"))
        self.assertEqual(after(argv, "-filter_complex"),
                         "[1:a]anull[a0];[2:a]anull[a1];[a0][a1]amix=inputs=2:duration=longest:normalize=0[aout]")
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        self.assertEqual(maps, ["0:v:0", "[aout]"])
        self.assertEqual(f.audio_track_layout(inputs(c.Config(), ("system", "mic")), "mix"),
                         [{"index": 0, "content": "system+mic"}])

    def test_both_separate_tracks_with_titles_and_gain(self):
        argv = self.argv(c.source_set("both"), mix="separate", mic_gain=1.5)
        self.assertEqual(after(argv, "-filter_complex"), "[1:a]anull[a0];[2:a]volume=1.5[a1]")
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        self.assertEqual(maps, ["0:v:0", "[a0]", "[a1]"])
        self.assertEqual(after(argv, "-metadata:s:a:0"), "title=system")
        self.assertEqual(after(argv, "-metadata:s:a:1"), "title=mic")

    def test_both_separate_without_filters_maps_directly(self):
        argv = self.argv(c.source_set("both"), mix="separate")
        self.assertNotIn("-filter_complex", argv)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "-map"], ["0:v:0", "1:a:0", "2:a:0"])

    def test_dshow_mic_is_shifted_in_the_samples(self):
        argv = self.argv(c.source_set("mic"), mic_backend="dshow")
        self.assertEqual(argv[argv.index("dshow") + 1:argv.index("dshow") + 5],
                         ["-audio_buffer_size", "50", "-i", f"audio={MIC}"])     # "" device -> the laptop's mic
        self.assertEqual(after(argv, "-filter_complex"), "[1:a]adelay=delays=300:all=1[a0]")
        argv = self.argv(c.source_set("mic"), mic_backend="dshow", offset_ms=-400, device="Headset")
        self.assertIn("audio=Headset", argv)
        self.assertEqual(after(argv, "-filter_complex"), "[1:a]atrim=start=0.100,asetpts=PTS-STARTPTS[a0]")

    def test_wasapi_offset_is_not_a_filter(self):
        # WASAPI streams are shifted by the anchor (recorder), so offset_ms adds no filter here
        argv = self.argv(c.source_set("system"), offset_ms=250)
        self.assertNotIn("-filter_complex", argv)

    def test_a_source_whose_child_failed_is_left_out(self):
        argv = f.build_capture_argv(f.spec_from_config(c.Config(), ffmpeg="ffmpeg", output="o.mkv",
                                                       audio_inputs=inputs(c.Config(), ("system", "mic"),
                                                                           {"mic": READY["mic"]})))
        self.assertNotIn("tcp://127.0.0.1:50001", argv)
        self.assertIn("tcp://127.0.0.1:50002", argv)
        self.assertNotIn("-filter_complex", argv)

    def test_other_formats_reach_the_raw_demuxer(self):
        r = {"system": CaptureReady(9, PcmFormat(44100, 6, 16, False), {}, 0.0)}
        argv = f.build_capture_argv(f.spec_from_config(c.Config(), ffmpeg="ffmpeg", output="o.mkv",
                                                       audio_inputs=inputs(c.Config(), ("system",), r)))
        self.assertEqual(argv[argv.index("-rw_timeout") + 2:argv.index("-rw_timeout") + 8],
                         ["-f", "s16le", "-ar", "44100", "-ac", "6"])

    def test_unknown_input_kind(self):
        with self.assertRaises(ValueError):
            f.AudioInput("x", "wasapi-direct").input_args(3.0)


class SourcesTest(unittest.TestCase):
    def test_effective_sources(self):
        a = c.AudioConfig()
        self.assertEqual(c.effective_sources(a), "system")
        self.assertEqual(c.effective_sources(a, "both"), "both")
        self.assertEqual(c.effective_sources(a, "both", no_mic=True), "system")
        self.assertEqual(c.effective_sources(a, "mic", no_mic=True), "none")
        self.assertEqual(c.effective_sources(a, None, no_mic=True), "system")
        off = c.AudioConfig(enabled=False)
        self.assertEqual(c.effective_sources(off), "none")
        self.assertEqual(c.effective_sources(off, "mic"), "mic")          # an explicit --audio wins
        with self.assertRaises(c.ConfigError):
            c.effective_sources(a, "speakers")

    def test_source_set_is_system_first(self):
        self.assertEqual(c.source_set("both"), ("system", "mic"))
        self.assertEqual(c.source_set("none"), ())
        self.assertEqual(c.source_set("mic"), ("mic",))


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
