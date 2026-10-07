"""Session C1b: the ffmpeg commands as pure functions. The cut's filtergraph
(one piece, joins with crossfades, no audio, separate tracks, a segment
missing a track, a size mismatch, concealed marks, calibration offsets),
the full argv (encoder per pipeline, chapters, -n, faststart, track titles),
the analysis commands and their parsers, and the invariant everything
rests on: the audio of the cut is exactly as long as its picture."""

import re
import unittest
from pathlib import Path

from tests import render_fixtures as rf
from peep import config as c, render

STEM = "2026-10-07-demo"
CFG = c.Config()


def plan_and(sc, pieces, xfades=(), offsets=None, concealed=(), chapters=()):
    plan = render.build_plan(sc, stem=STEM)
    res = render.Resolved(pieces=pieces, xfades=list(xfades), audio_offsets=offsets or {}, boundaries=[], seams=[],
                          chapters=list(chapters), marks_excluded=[], calibration=[], fallbacks=[], dropped=[],
                          concealed=list(concealed))
    return plan, res


class FiltergraphTest(unittest.TestCase):
    def test_one_piece_no_concat(self):
        plan, res = plan_and(rf.build_sidecar(STEM, [20.0]), [render.Piece(1, None, 30, 581, 30)])
        graph, maps = render.build_filtergraph(plan, res, {1: 0}, "qsv")
        self.assertIn("[0:v:0]trim=start=0.983333:end=19.350000,setpts=PTS-STARTPTS,setsar=1[v0]", graph)
        self.assertIn("[v0]format=nv12[vout]", graph)
        self.assertIn("[0:a:0]atrim=start=1.000000:end=19.366667,asetpts=PTS-STARTPTS,apad=whole_dur=18.366667[a0_0]",
                      graph)
        self.assertIn("[a0_0]anull[aout0]", graph)
        self.assertNotIn("concat", graph)
        self.assertEqual(maps, ["-map", "[vout]", "-map", "[aout0]"])

    def test_joins_concat_the_picture_and_crossfade_the_audio(self):
        sc = rf.build_sidecar(STEM, [20.0, 15.0])
        pieces = [render.Piece(1, None, 30, 300, 30), render.Piece(2, None, 60, 400, 30)]
        plan, res = plan_and(sc, pieces, xfades=[0.04])
        graph, maps = render.build_filtergraph(plan, res, {1: 0, 2: 1}, "x264")
        self.assertIn("[v0][v1]concat=n=2:v=1:a=0,format=yuv420p[vout]", graph)
        # half the crossfade beyond each joined edge, nothing at the outer edges
        self.assertIn("[0:a:0]atrim=start=1.000000:end=10.020000", graph)
        self.assertIn("[1:a:0]atrim=start=1.980000:end=13.333333", graph)
        self.assertIn("[a0_0][a0_1]acrossfade=d=0.040000:c1=tri:c2=tri[x0_1]", graph)

    def test_audio_is_exactly_as_long_as_the_picture(self):
        sc = rf.build_sidecar(STEM, [20.0, 15.0, 12.0])
        pieces = [render.Piece(1, None, 30, 301, 30), render.Piece(2, None, 33, 377, 30),
                  render.Piece(3, None, 41, 310, 30)]
        for xf in ([0.04, 0.04], [0.04, 0.0], [0.012, 0.3]):
            plan, res = plan_and(sc, pieces, xfades=xf)
            graph, _ = render.build_filtergraph(plan, res, {1: 0, 2: 1, 3: 2}, "qsv")
            lengths = [float(x) for x in re.findall(r"apad=whole_dur=([\d.]+)", graph)]
            video = sum(p.duration_s for p in pieces)
            self.assertAlmostEqual(sum(lengths) - sum(xf), video, delta=3e-6, msg=xf)
            if 0.0 in xf:
                self.assertIn("concat=n=2:v=0:a=1", graph)     # a join with no room: plain concat

    def test_no_audio_maps_only_the_picture(self):
        plan, res = plan_and(rf.build_sidecar(STEM, [20.0], tracks=()), [render.Piece(1, None, 30, 581, 30)])
        graph, maps = render.build_filtergraph(plan, res, {1: 0}, "qsv")
        self.assertNotIn(":a:", graph)
        self.assertEqual(maps, ["-map", "[vout]"])

    def test_separate_tracks_each_get_the_same_cuts(self):
        sc = rf.build_sidecar(STEM, [20.0, 15.0], tracks=("system", "mic"))
        pieces = [render.Piece(1, None, 30, 300, 30), render.Piece(2, None, 60, 400, 30)]
        plan, res = plan_and(sc, pieces, xfades=[0.04])
        graph, maps = render.build_filtergraph(plan, res, {1: 0, 2: 1}, "qsv")
        self.assertIn("[0:a:1]atrim=start=1.000000:end=10.020000", graph)
        self.assertIn("[a1_0][a1_1]acrossfade", graph)
        self.assertEqual(maps, ["-map", "[vout]", "-map", "[aout0]", "-map", "[aout1]"])

    def test_a_segment_without_a_track_contributes_silence(self):
        sc = rf.build_sidecar(STEM, [20.0, 15.0], tracks=("system", "mic"))
        sc["segments"][1]["audio"]["tracks"] = [{"index": 0, "content": "system"}]     # the mic child failed
        pieces = [render.Piece(1, None, 30, 300, 30), render.Piece(2, None, 60, 400, 30)]
        plan, res = plan_and(sc, pieces, xfades=[0.04])
        graph, _ = render.build_filtergraph(plan, res, {1: 0, 2: 1}, "qsv")
        self.assertIn("anullsrc=r=48000:cl=stereo,atrim=duration=11.353333[a1_1]", graph)

    def test_a_segment_of_another_size_is_scaled(self):
        sc = rf.build_sidecar(STEM, [20.0, 15.0])
        sc["segments"][1]["video"]["output_size"] = "1920x1200"
        pieces = [render.Piece(1, None, 30, 300, 30), render.Piece(2, None, 60, 400, 30)]
        plan, res = plan_and(sc, pieces, xfades=[0.04])
        graph, _ = render.build_filtergraph(plan, res, {1: 0, 2: 1}, "qsv")
        self.assertIn("setpts=PTS-STARTPTS,scale=2560:1600,setsar=1[v1]", graph)
        self.assertNotIn("scale=2560:1600[v0]", graph)

    def test_calibration_offsets_move_the_audio_window(self):
        plan, res = plan_and(rf.build_sidecar(STEM, [20.0]), [render.Piece(1, None, 30, 581, 30)],
                             offsets={1: 0.085})
        graph, _ = render.build_filtergraph(plan, res, {1: 0}, "qsv")
        self.assertIn("atrim=start=1.085000:end=19.451667", graph)
        plan, res = plan_and(rf.build_sidecar(STEM, [20.0]), [render.Piece(1, None, 0, 581, 30)],
                             offsets={1: -0.05})
        graph, _ = render.build_filtergraph(plan, res, {1: 0}, "qsv")
        self.assertIn("atrim=start=0.000000:end=19.316667,asetpts=PTS-STARTPTS,adelay=delays=50:all=1", graph)

    def test_concealed_patches_hold_the_corner_and_full_flashes_the_frame(self):
        plan, res = plan_and(rf.build_sidecar(STEM, [20.0]), [render.Piece(1, None, 30, 581, 30)], concealed=[
            {"piece": 0, "first": 100, "last": 105, "replace": 99, "rect": [202, 202, 0, 1398]},
            {"piece": 0, "first": 300, "last": 305, "replace": 299, "rect": None}])
        graph, _ = render.build_filtergraph(plan, res, {1: 0}, "qsv")
        self.assertIn("setpts=PTS-STARTPTS,setsar=1[v0c0]", graph)
        self.assertIn("[v0c0]split=3[v0m0][v0a0][v0b0];[v0a0][v0b0]freezeframes=first=100:last=105:replace=99,"
                      "crop=202:202:0:1398[v0p0];[v0m0][v0p0]overlay=0:1398:enable='between(n,100,105)'[v0c1]",
                      graph)
        self.assertIn("[v0c1]split=2[v0m1][v0f1];[v0m1][v0f1]freezeframes=first=300:last=305:replace=299[v0]",
                      graph)
        self.assertIn("[v0]format=nv12[vout]", graph)


class ArgvTest(unittest.TestCase):
    def argv(self, pipeline="qsv", chapters=True, tracks=("system", "mic")):
        sc = rf.build_sidecar(STEM, [20.0, 15.0], tracks=tracks)
        pieces = [render.Piece(1, None, 30, 300, 30), render.Piece(2, None, 60, 400, 30)]
        plan, res = plan_and(sc, pieces, xfades=[0.04])
        return render.build_render_argv(plan, res, cfg=CFG, ffmpeg="ffmpeg.exe", folder=Path("C:/v/inbox"),
                                        output=Path("C:/v/inbox") / f"{STEM}.cut.part.mp4", pipeline=pipeline,
                                        meta_path=Path("C:/v/inbox") / f"{STEM}.cut.part.ffmeta" if chapters else None)

    def test_inputs_outputs_and_never_overwrite(self):
        a = self.argv()
        self.assertEqual(a[:9], ["ffmpeg.exe", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "warning", "-progress",
                                 "pipe:1", "-n"])
        ins = [a[i + 1] for i, x in enumerate(a) if x == "-i"]
        self.assertEqual([Path(x).name for x in ins], [f"{STEM}.mp4", f"{STEM}.seg2.mp4", f"{STEM}.cut.part.ffmeta"])
        self.assertEqual(a[a.index("-map_chapters") + 1], "2")
        self.assertEqual(a[-4:], ["+faststart", "-f", "mp4", str(Path("C:/v/inbox") / f"{STEM}.cut.part.mp4")])

    def test_the_originals_encoder_settings(self):
        a = self.argv("qsv")
        i = a.index("-c:v")
        self.assertEqual(a[i:i + 8], ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "25", "-g", "60"])
        self.assertIn("format=nv12[vout]", a[a.index("-filter_complex") + 1])
        a = self.argv("x264")
        i = a.index("-c:v")
        self.assertEqual(a[i:i + 8], ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "60"])
        self.assertIn("format=yuv420p[vout]", a[a.index("-filter_complex") + 1])
        self.assertEqual(a[a.index("-fps_mode"):a.index("-fps_mode") + 4], ["-fps_mode", "cfr", "-r", "30"])

    def test_track_layout_kept(self):
        a = self.argv()
        self.assertEqual(a[a.index("-c:a"):a.index("-c:a") + 4], ["-c:a", "aac", "-b:a", "160k"])
        self.assertIn("-metadata:s:a:0", a)
        self.assertEqual(a[a.index("-metadata:s:a:1") + 1], "title=mic")
        one = self.argv(tracks=("system",))
        self.assertNotIn("-metadata:s:a:0", one)
        none = self.argv(tracks=())
        self.assertNotIn("-c:a", none)

    def test_no_chapters_maps_none(self):
        a = self.argv(chapters=False)
        self.assertEqual(a[a.index("-map_chapters") + 1], "-1")
        self.assertNotIn("ffmetadata", a)


class ChaptersTest(unittest.TestCase):
    def test_ffmetadata(self):
        text = render.ffmetadata([{"title": "start", "start_s": 0.0, "end_s": 4.9},
                                  {"title": "a=b; #c\\d", "start_s": 4.9, "end_s": 9.667}])
        self.assertEqual(text, ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=4900\ntitle=start\n"
                               "[CHAPTER]\nTIMEBASE=1/1000\nSTART=4900\nEND=9667\ntitle=a\\=b\\; \\#c\\\\d\n")

    def test_map_marks(self):
        pieces = [render.Piece(1, 1, 30, 300, 30, 0.0), render.Piece(2, 2, 30, 330, 30, 9.0)]
        marks = [{"segment": 1, "media_s": 5.0, "label": "", "after": False},
                 {"segment": 1, "media_s": None, "label": "resume", "after": True},     # pressed while paused
                 {"segment": 1, "media_s": 12.0, "label": "cut out", "after": False},   # after the last piece
                 {"segment": 2, "media_s": 0.2, "label": "early", "after": False},      # just before a piece
                 {"segment": 2, "media_s": 3.0, "label": "two", "after": False}]
        ch, ex = render.map_marks(marks, pieces)
        self.assertEqual([(x["title"], x["start_s"], x["end_s"]) for x in ch],
                         [("start", 0.0, 4.0), ("mark 1", 4.0, 9.0), ("resume · early", 9.0, 11.0),
                          ("two", 11.0, 19.0)])
        self.assertEqual([e["label"] for e in ex], ["cut out"])
        self.assertEqual(render.map_marks([], pieces), ([], []))


class AnalysisCommandsTest(unittest.TestCase):
    def test_frames_argv_keeps_timestamps(self):
        a = render.frames_argv("ffmpeg", "x.mp4", 0.5, 2.0, (140, 140, 30, 1430), (4, 4))
        self.assertIn("-copyts", a)
        self.assertEqual(a[a.index("-ss") + 1], "0.500")
        self.assertEqual(a[a.index("-vf") + 1], "crop=140:140:30:1430,scale=4:4:flags=area,format=rgb24,showinfo")
        self.assertEqual(a[-3:], ["-f", "rawvideo", "-"])
        full = render.frames_argv("ffmpeg", "x.mp4", 0.0, 2.0, None, (16, 10))
        self.assertEqual(full[full.index("-vf") + 1], "scale=16:10:flags=area,format=rgb24,showinfo")

    def test_pcm_and_volume_argv_mix_every_track(self):
        a = render.pcm_argv("ffmpeg", "x.mp4", 1.0, 0.5, 2, None)
        self.assertEqual(a[a.index("-filter_complex") + 1], "[0:a:0][0:a:1]amix=inputs=2:normalize=0[m]")
        self.assertEqual(a[-7:], ["-ac", "1", "-ar", "16000", "-f", "s16le", "-"])
        one = render.pcm_argv("ffmpeg", "x.mp4", 1.0, 0.5, 2, 1)
        self.assertEqual(one[one.index("-map") + 1], "0:a:1")
        v = render.volume_argv("ffmpeg", "x.mp4", 1.0, 5.0, 1)
        self.assertEqual(v[v.index("-af") + 1], "volumedetect")
        self.assertEqual(v[-3:], ["-f", "null", "-"])

    def test_parse_frames(self):
        raw = bytes([253, 0, 252]) * 160 + bytes([60, 60, 60]) * 160
        err = b"[Parsed_showinfo_2 @ 0] n:   0 pts:  12 pts_time:0.8 x\n[Parsed_showinfo_2 @ 0] n: 1 pts_time:0.833333\n"
        self.assertEqual(render.parse_frames(raw, err, (16, 10), 0.5, 30, median=False),
                         [(0.8, (253, 0, 252)), (0.833333, (60, 60, 60))])
        # no showinfo line: placed by position
        self.assertEqual(render.parse_frames(raw[:480], b"", (16, 10), 0.5, 30, median=True), [(0.5, (253, 0, 252))])


if __name__ == "__main__":
    unittest.main()
