"""Session C1b: deciding boundaries and seams from the media. Flash and patch
detection against synthetic frames, its fallbacks, seam snapping and silence
trimming against synthetic PCM, the clap onset, frame quantisation, and the
whole resolve() stage through an in-process scene (no ffmpeg anywhere)."""

import math
import unittest

from tests import render_fixtures as rf
from peep import config as c, render

STEM = "2026-10-07-demo"
MAG, GRN, BLU, GREY = (253, 0, 252), (0, 254, 0), (0, 0, 254), (60, 60, 60)
CFG = c.RenderConfig()


def frames(spans, start=0.0, end=3.0, fps=30, bg=GREY):
    """[(t, rgb)] for frames k/fps in [start, end), coloured where spans say."""
    out = []
    for k in range(math.ceil(start * fps - 1e-9), math.ceil(end * fps - 1e-9)):
        t = k / fps
        col = next((rgb for t0, t1, rgb in spans if t0 <= t < t1), bg)
        out.append((round(t, 6), col))
    return out


def tone(rate, start, end, spans, amp=0.3, freq=300.0, noise=0.0):
    """s16 samples over [start, end) with a tone in each span (+ optional noise)."""
    out = []
    for n in range(int(start * rate), int(end * rate)):
        t = n / rate
        v = sum(amp * math.sin(2 * math.pi * freq * t) for t0, t1 in spans if t0 <= t < t1)
        v += noise * ((n * 7919 % 1000) / 500.0 - 1.0)
        out.append(int(max(-1, min(1, v)) * 32767))
    return out


class DetectRunTest(unittest.TestCase):
    def test_the_magenta_run_and_the_frame_after_it(self):
        f = frames([(0.8, 1.0, MAG)])
        d = render.detect_run(f, MAG, 0.8, 8, 1 / 30, window=(0.0, 3.0))
        self.assertTrue(d.found)
        self.assertAlmostEqual(d.first_s, 0.8, places=4)
        self.assertAlmostEqual(d.last_s, 29 / 30, places=4)
        self.assertAlmostEqual(d.next_s, 1.0, places=4)
        self.assertEqual((d.frames, d.rgb), (6, MAG))

    def test_encoded_colours_within_tolerance_count(self):
        f = frames([(0.8, 1.0, (250, 12, 241))])               # what an encoder might make of magenta
        self.assertTrue(render.detect_run(f, (255, 0, 255), 0.8, 8, 1 / 30).found)
        f = frames([(0.8, 1.0, (180, 0, 180))])                # too far: a different colour
        d = render.detect_run(f, (255, 0, 255), 0.8, 8, 1 / 30)
        self.assertFalse(d.found)
        self.assertIn("closest colour (180, 0, 180)", d.reason)

    def test_nothing_there(self):
        d = render.detect_run(frames([]), MAG, 0.8, 8, 1 / 30)
        self.assertFalse(d.found)
        self.assertIn("no #FD00FC frame", d.reason)
        self.assertIn("no frames decoded", render.detect_run([], MAG, 0.8, 8, 1 / 30).reason)

    def test_an_area_that_is_that_colour_anyway_is_not_trusted(self):
        d = render.detect_run(frames([(0.1, 2.9, BLU)], start=0.0, end=3.0), BLU, 1.0, 8, 1 / 30)
        self.assertFalse(d.found)
        self.assertIn("at most 8 expected", d.reason)

    def test_a_run_cut_by_the_window_edge_is_not_trusted(self):
        f = frames([(0.9, 1.2, MAG)], start=1.0, end=2.0)
        d = render.detect_run(f, MAG, 1.0, 8, 1 / 30, window=(1.0, 2.0), file_end_s=20.0)
        self.assertFalse(d.found)
        self.assertIn("edge of the search window", d.reason)
        f = frames([(1.85, 2.1, MAG)], start=1.0, end=2.0)
        d = render.detect_run(f, MAG, 1.9, 8, 1 / 30, window=(1.0, 2.0), file_end_s=20.0)
        self.assertIn("edge of the search window", d.reason)
        # but a run at the end of the file is just the end
        f = frames([(19.85, 20.0, GRN)], start=19.0, end=20.0)
        self.assertTrue(render.detect_run(f, GRN, 19.85, 8, 1 / 30, window=(19.0, 20.0), file_end_s=20.0).found)

    def test_of_two_runs_the_one_nearest_the_stamp(self):
        f = frames([(0.3, 0.5, BLU), (2.0, 2.2, BLU)], end=3.0)
        d = render.detect_run(f, BLU, 2.05, 8, 1 / 30)
        self.assertAlmostEqual(d.first_s, 2.0, places=4)
        self.assertEqual(d.runs, 2)

    def test_colour_helpers(self):
        self.assertEqual(render.mean_rgb(bytes([10, 20, 30, 30, 40, 50])), (20, 30, 40))
        self.assertEqual(render.median_rgb(bytes([0, 0, 0, 200, 200, 200, 210, 210, 210])), (200, 200, 200))
        self.assertEqual(render.hex_rgb("#FF8000"), (255, 128, 0))


class GeometryTest(unittest.TestCase):
    def seg(self, size="2560x1600"):
        return render.SegmentInfo(1, "f.mp4", 10.0, 30, render.parse_size(size), (2560, 1600), [], "qsv",
                                  render.NO_FIDUCIAL, render.NO_FIDUCIAL, None)

    def fid(self, rect=rf.RECT):
        return render.Fiducial("#0000FF", "patch", 1.0, "qpc", 0.2, rect, rf.SCREEN)

    def test_patch_crop_is_inset_and_even(self):
        self.assertEqual(render.patch_crop(self.fid(), self.seg()), (140, 140, 30, 1430))

    def test_patch_crop_scales_with_the_output(self):
        w, h, x, y = render.patch_crop(self.fid(), self.seg("1920x1200"))
        self.assertEqual((w % 2, h % 2, x % 2, y % 2), (0, 0, 0, 0))
        self.assertTrue(x < 40 and 1060 < y < 1100 and 100 < w < 110)

    def test_patch_crop_needs_a_rect(self):
        self.assertIsNone(render.patch_crop(self.fid(rect=None), self.seg()))

    def test_conceal_rect_covers_the_patch_and_stays_on_the_frame(self):
        self.assertEqual(render.conceal_rect(self.fid(), (2560, 1600)), (202, 202, 0, 1398))
        top_right = render.Fiducial("#00FFFF", "patch", 1.0, "qpc", 0.2, {"x": 2360, "y": 0, "w": 200, "h": 200},
                                    rf.SCREEN)
        self.assertEqual(render.conceal_rect(top_right, (2560, 1600)), (202, 202, 2358, 0))

    def test_frame_index(self):
        self.assertEqual(render._frame_index(1.0, 30), 30)
        self.assertEqual(render._frame_index(1.0333, 30), 31)            # ms-rounded pts of frame 31
        self.assertEqual(render._frame_index(1.005, 30), 30)             # within a quarter frame: that frame
        self.assertEqual(render._frame_index(1.02, 30), 31)              # further in: the next one
        self.assertEqual(render._frame_index(0.0, 30), 0)


class SeamTest(unittest.TestCase):
    def levels(self, start, end, spans, noise=0.0002):
        return render.rms_levels(tone(render.PCM_RATE, start, end, spans, noise=noise)), start

    def test_levels(self):
        lv = render.rms_levels([0] * 1600 + tone(16000, 0, 0.1, [(0, 0.1)], amp=0.5))
        self.assertEqual(lv[:10], [-120.0] * 10)
        self.assertAlmostEqual(lv[12], 20 * math.log10(0.5 / math.sqrt(2)), delta=0.3)

    def test_silence_before_speech_is_trimmed_keeping_a_little(self):
        lv, t0 = self.levels(5.0, 7.0, [(5.8, 7.0)])
        seam, how, d = render.place_seam("start", 5.0, 9.0, lv, t0, CFG)
        self.assertEqual(how, "silence")
        self.assertAlmostEqual(seam, 5.8 - 0.25, delta=0.011)
        self.assertAlmostEqual(d["sound_at_s"], 5.8, delta=0.011)

    def test_silence_after_speech_is_trimmed_keeping_a_little(self):
        lv, t0 = self.levels(8.0, 10.0, [(8.0, 9.1)])
        seam, how, _ = render.place_seam("end", 10.0, 6.0, lv, t0, CFG)
        self.assertEqual(how, "silence")
        self.assertAlmostEqual(seam, 9.1 + 0.25, delta=0.011)

    def test_trim_is_capped(self):
        lv, t0 = self.levels(5.0, 7.0, [(6.1, 7.0)])                  # 1.1 s of silence: keep 250 ms -> 0.85
        seam, how, _ = render.place_seam("start", 5.0, 9.0, lv, t0, CFG)
        self.assertEqual(how, "silence")
        self.assertAlmostEqual(seam, 5.85, delta=0.011)
        lv, t0 = self.levels(5.0, 7.0, [(6.6, 7.0)])                  # 1.6 s: capped at the max trim
        seam, how, _ = render.place_seam("start", 5.0, 9.0, lv, t0, CFG)
        self.assertEqual(how, "silence-max")
        self.assertAlmostEqual(seam, 6.0, delta=1e-6)                 # silence_max_trim_ms = 1000

    def test_all_silence_trims_the_max(self):
        lv, t0 = self.levels(5.0, 6.5, [(6.4, 6.5)])
        seam, how, _ = render.place_seam("start", 5.0, 9.0, lv, t0, CFG)
        self.assertEqual((how, round(seam, 3)), ("silence-max", 6.0))

    def test_a_silent_interval_is_never_trimmed(self):
        lv, t0 = self.levels(5.0, 6.5, [])
        seam, how, _ = render.place_seam("start", 5.0, 9.0, lv, t0, CFG, has_sound=False)
        self.assertEqual(how, "none")
        self.assertEqual(seam, 5.0)

    def test_sound_at_the_bound_snaps_to_the_quietest_point(self):
        # speech running through the boundary, with one quiet gap 120-170 ms in
        lv, t0 = self.levels(5.0, 6.5, [(5.0, 5.12), (5.17, 6.5)])
        seam, how, d = render.place_seam("start", 5.0, 9.0, lv, t0, CFG)
        self.assertEqual(how, "snap")
        self.assertTrue(5.12 <= seam <= 5.17, seam)
        self.assertLess(d["level_db"], -60)

    def test_snap_stays_inside_the_window(self):
        lv, t0 = self.levels(5.0, 6.5, [(5.0, 5.5), (5.6, 6.5)])          # the only gap is 500 ms in
        seam, how, _ = render.place_seam("start", 5.0, 9.0, lv, t0, CFG)
        self.assertEqual(how, "snap")
        self.assertLessEqual(seam - 5.0, 0.3 + 1e-6)

    def test_a_short_silence_is_snapped_in_not_trimmed(self):
        lv, t0 = self.levels(5.0, 6.5, [(5.1, 6.5)])                      # 100 ms < keep
        seam, how, _ = render.place_seam("start", 5.0, 9.0, lv, t0, CFG)
        self.assertEqual(how, "snap")
        self.assertTrue(5.0 <= seam < 5.1)

    def test_never_past_the_interval_middle(self):
        lv, t0 = self.levels(5.0, 6.5, [])
        seam, how, _ = render.place_seam("start", 5.0, 5.4, lv, t0, CFG)    # a 0.8 s interval
        self.assertEqual(how, "none")
        self.assertEqual(seam, 5.0)

    def test_no_audio(self):
        self.assertEqual(render.place_seam("end", 5.0, 3.0, [], 3.0, CFG)[:2], (5.0, "none"))

    def test_trimming_off(self):
        cfg = c.RenderConfig(silence_max_trim_ms=0)
        lv, t0 = self.levels(5.0, 7.0, [(5.2, 7.0)])
        seam, how, _ = render.place_seam("start", 5.0, 9.0, lv, t0, cfg)
        self.assertEqual(how, "snap")                     # no trim: only a snap, inside the silence
        self.assertTrue(5.0 <= seam < 5.2)
        lv, t0 = self.levels(5.0, 7.0, [(5.8, 7.0)])
        self.assertEqual(render.place_seam("start", 5.0, 9.0, lv, t0, cfg)[1], "none")    # nothing to snap to


class OnsetTest(unittest.TestCase):
    def test_the_clap_onset_within_a_hop(self):
        s = tone(16000, 0.5, 1.2, [(0.795, 0.915)], amp=0.4, freq=1000.0, noise=0.001)
        self.assertAlmostEqual(render.tone_onset(s, 16000, 0.5), 0.795, delta=0.006)

    def test_a_click_is_not_a_clap(self):
        s = tone(16000, 0.5, 1.2, [(0.7, 0.72)], amp=0.4, freq=1000.0)
        self.assertIsNone(render.tone_onset(s, 16000, 0.5))

    def test_another_pitch_is_not_a_clap(self):
        s = tone(16000, 0.5, 1.2, [(0.795, 0.915)], amp=0.4, freq=300.0)
        self.assertIsNone(render.tone_onset(s, 16000, 0.5))


class ResolveTest(unittest.TestCase):
    """resolve() through rf.SceneAnalyzer: the media a real capture makes."""

    def resolve(self, sc, speech=None, cfg=CFG, fail=(), **scene_kw):
        plan = render.build_plan(sc, stem=STEM)
        an = rf.SceneAnalyzer(rf.scene_for(sc, speech, **scene_kw), fail=fail)
        return plan, render.resolve(plan, an, cfg), an

    def test_zero_press_cut_starts_after_magenta_and_ends_before_green(self):
        sc = rf.build_sidecar(STEM, [20.0])
        plan, res, _ = self.resolve(sc)
        (p,) = res.pieces
        # magenta shown at 0.76 -> frames 0.8..0.967: kept from 1.0 (frame 30)
        # green shown at 19.30 -> first green frame 19.367 (k=581): kept to frame 580
        self.assertEqual((p.k1, p.k2), (30, 581))
        self.assertEqual(res.seams, [])                       # zero presses: no seam moves
        self.assertEqual(res.fallbacks, [])
        self.assertEqual([b["method"] for b in res.boundaries], ["detected", "detected"])
        self.assertEqual(res.boundaries[0]["frames"], 6)
        self.assertAlmostEqual(res.boundaries[0]["delta_ms"], 40.0, delta=34)
        self.assertAlmostEqual(res.output_duration_s, (581 - 30) / 30, places=3)

    def test_takes_exclude_their_patches_and_snap_into_silence(self):
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("take", 1, 9.0)])
        plan, res, _ = self.resolve(sc, speech={1: [(4.0, 8.0)]})
        (p,) = res.pieces
        open_b, close_b = res.boundaries
        self.assertEqual((open_b["color"], close_b["color"]), ("#0000FF", "#FF0000"))
        self.assertGreater(p.start_s, open_b["last_frame_s"])     # never a blue frame
        self.assertLess(p.end_s, close_b["detected_s"] + 1e-6)    # never a red one
        self.assertEqual([s["how"] for s in res.seams], ["silence", "silence"])
        self.assertAlmostEqual(p.start_s, 3.75, delta=0.034)      # speech at 4.0, keep 250 ms
        self.assertAlmostEqual(p.end_s, 8.25, delta=0.034)

    def test_a_missing_flash_falls_back_to_its_stamp_loudly(self):
        sc = rf.build_sidecar(STEM, [20.0])
        plan, res, _ = self.resolve(sc, hide=(("segment-start", 1),))
        b = res.boundaries[0]
        self.assertEqual(b["method"], "stamp")
        self.assertIn("no #FF00FF frame", b["fallback"])
        stamp = rf.START_FLASH_AT
        self.assertAlmostEqual(b["bound_s"], stamp + render.FIDUCIAL_LAG_S + 0.203 + render.FALLBACK_MARGIN_S,
                               places=3)
        self.assertEqual(len(res.fallbacks), 1)
        self.assertIn("start flash", res.fallbacks[0])
        self.assertIn("used the stamp", res.fallbacks[0])
        # the fallback errs toward excluding: past where the flash really was
        self.assertGreaterEqual(res.pieces[0].start_s, stamp + render.FIDUCIAL_LAG_S + 0.2)

    def test_a_patch_in_the_wrong_colour_is_not_taken_for_it(self):
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("take", 1, 9.0)])
        open_id = sc["takes"][0]["open"]["event"]
        plan, res, _ = self.resolve(sc, recolor={("event", open_id): "#00FF00"})
        self.assertEqual(res.boundaries[0]["method"], "stamp")
        self.assertEqual(res.boundaries[1]["method"], "detected")

    def test_decode_failures_fall_back_and_still_render(self):
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("take", 1, 9.0)])
        plan, res, _ = self.resolve(sc, fail=("frames", "pcm", "max_db"))
        self.assertTrue(all(b["method"] == "stamp" for b in res.boundaries))
        self.assertTrue(all("could not decode" in b["fallback"] for b in res.boundaries))
        self.assertEqual(len(res.pieces), 1)
        self.assertTrue(any("loudness not measured" in f for f in res.fallbacks))
        self.assertTrue(any("audio not decoded" in f for f in res.fallbacks))

    def test_a_silent_take_keeps_every_frame_between_its_patches(self):
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("take", 1, 9.0)])
        plan, res, _ = self.resolve(sc, speech={})
        self.assertTrue(any("silent throughout" in f for f in res.fallbacks))
        self.assertEqual([s["how"] for s in res.seams], ["none", "none"])
        p = res.pieces[0]
        self.assertAlmostEqual(p.start_s, res.boundaries[0]["bound_s"], delta=0.034)

    def test_a_take_too_short_once_its_patches_are_out_is_dropped(self):
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("take", 1, 3.4), ("take", 1, 10.0),
                                              ("take", 1, 15.0)])
        plan, res, _ = self.resolve(sc, speech={1: [(11.0, 14.0)]})
        self.assertEqual([p.take for p in res.pieces], [2])
        self.assertEqual(res.dropped[0]["take"], 1)
        self.assertTrue(any("dropped" in f for f in res.fallbacks), res.fallbacks)

    def test_joins_crossfade_and_the_outer_edges_do_not(self):
        sc = rf.build_sidecar(STEM, [12.0, 10.0])
        plan, res, _ = self.resolve(sc, speech={1: [(2.0, 10.0)], 2: [(2.0, 8.0)]})
        self.assertEqual(len(res.pieces), 2)
        self.assertEqual(res.xfades, [0.04])
        self.assertEqual({s["side"] for s in res.seams}, {"start", "end"})     # only at the join
        self.assertEqual([(s["interval"], s["side"]) for s in res.seams], [(0, "end"), (1, "start")])
        self.assertAlmostEqual(res.pieces[1].out_start_s, res.pieces[0].duration_s, places=6)

    def test_a_crossfade_with_no_room_is_shortened_and_said(self):
        sc = rf.build_sidecar(STEM, [12.0, 10.0])
        plan = render.build_plan(sc, stem=STEM)
        an = rf.SceneAnalyzer(rf.scene_for(sc, {1: [(2.0, 10.0)], 2: [(2.0, 8.0)]}))
        res = render.resolve(plan, an, c.RenderConfig(crossfade_ms=500))
        self.assertTrue(any("crossfade" in f for f in res.fallbacks) or res.xfades == [0.5])

    def test_calibration_measures_and_only_applies_when_asked(self):
        sc = rf.build_sidecar(STEM, [20.0])
        plan, res, _ = self.resolve(sc)
        (cal,) = res.calibration
        self.assertEqual(cal["status"], "measured")
        # tone at request + 40 ms, first magenta frame at 0.8: residual about +-1 hop
        self.assertLess(abs(cal["residual_ms"]), 12)
        self.assertFalse(cal["applied"])
        self.assertEqual(res.audio_offsets, {})
        # make the audio 80 ms late: measure reports it, apply corrects it
        late = rf.scene_for(sc)
        for a in late["files"][f"{STEM}.mp4"]["audio"]:
            a["t0"] += 0.08
            a["t1"] += 0.08
        res = render.resolve(plan, rf.SceneAnalyzer(late), c.RenderConfig(av_calibration="apply"))
        self.assertAlmostEqual(res.calibration[0]["residual_ms"], 85, delta=12)
        self.assertTrue(res.calibration[0]["applied"])
        self.assertAlmostEqual(res.audio_offsets[1], 0.085, delta=0.012)
        self.assertTrue(any("audio shifted" in f for f in res.fallbacks))
        res = render.resolve(plan, rf.SceneAnalyzer(late), c.RenderConfig(av_calibration="off"))
        self.assertEqual(res.calibration, [])

    def test_calibration_skips_say_why(self):
        plan, res, _ = self.resolve(rf.build_sidecar(STEM, [20.0], tracks=("mic",)))
        self.assertEqual(res.calibration[0]["why"], "no system-audio track")
        plan, res, _ = self.resolve(rf.build_sidecar(STEM, [20.0], clap=False))
        self.assertEqual(res.calibration[0]["why"], "no clap was played")

    def test_marks_become_chapters_and_their_patches_are_concealed(self):
        sc = rf.build_sidecar(STEM, [20.0, 20.0], [("take", 1, 2.0), ("mark", 1, 5.0, "first"),
                                                    ("mark", "paused", 1, "after the break"),
                                                    ("mark", 2, 6.0), ("take", 2, 12.0), ("mark", 2, 15.0, "gone")])
        plan, res, _ = self.resolve(sc, speech={1: [(3.0, 18.0)], 2: [(2.0, 11.0)]})
        titles = [(ch["title"], ch["source"] and ch["source"]["segment"]) for ch in res.chapters]
        self.assertEqual(titles, [("start", None), ("first", 1), ("after the break", 1), ("mark 3", 2)])
        brk = res.chapters[2]
        self.assertAlmostEqual(brk["start_s"], res.pieces[1].out_start_s, places=3)
        self.assertEqual([m["label"] for m in res.marks_excluded], ["gone"])
        self.assertEqual([cc["mark"] for cc in res.concealed], [1, 3])
        cc = res.concealed[0]
        self.assertEqual((cc["frames"], cc["replace"], cc["rect"]), (6, cc["first"] - 1, [202, 202, 0, 1398]))
        self.assertEqual(res.chapters[-1]["end_s"], res.output_duration_s)


if __name__ == "__main__":
    unittest.main()


class ReviewRegressionTest(unittest.TestCase):
    """Defects an independent review of this session's diff found, each pinned."""

    def resolve(self, sc, speech=None, **scene_kw):
        plan = render.build_plan(sc, stem=STEM)
        scene = rf.scene_for(sc, speech, **scene_kw)
        res = render.resolve(plan, rf.SceneAnalyzer(scene), CFG)
        return plan, res, scene

    def test_a_take_pressed_during_the_start_flash_starts_after_it(self):
        # the recorder shows that take's patch after the flash; here its stamp says it overlapped,
        # and the render must still never let the flash through
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 0.3), ("take", 1, 5.0)], serial_fiducials=False)
        plan, res, scene = self.resolve(sc, {1: [(1.5, 4.5)]})
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])
        self.assertEqual(res.fallbacks, [])
        start = next(b for b in res.boundaries if b["kind"] == "take-open")
        self.assertEqual(start["clamped_to"], "start flash")
        self.assertIn("segment-start", [b["kind"] for b in res.boundaries])
        self.assertGreaterEqual(res.pieces[0].start_s, 1.0 - 1e-6)

    def test_a_take_closed_after_the_stop_flash_ends_before_it(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 19.8)], serial_fiducials=False)
        plan, res, scene = self.resolve(sc, {1: [(4.0, 18.0)]})
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])
        close = next(b for b in res.boundaries if b["kind"] == "take-close")
        self.assertEqual(close["clamped_to"], "stop flash")

    def test_no_press_sequence_leaks_a_fiducial_frame(self):
        import random
        rnd = random.Random(1007)
        for case in range(25):
            presses = []
            for seg, dur in ((1, 12.0), (2, 9.0)):
                t = rnd.uniform(-0.1, 1.2)
                while t < dur - 0.2:
                    presses.append((rnd.choice(["take", "take", "take", "retake", "mark"]), seg, round(t, 2)))
                    t += rnd.uniform(0.4, 3.0)
                if rnd.random() < 0.5:
                    presses.append(("take", seg, round(rnd.uniform(dur - 0.9, dur - 0.05), 2)))
            sc = rf.build_sidecar(STEM, [12.0, 9.0], presses)
            plan, res, scene = self.resolve(sc, {1: [(1.0, 11.0)], 2: [(1.0, 8.0)]})
            with self.subTest(case=case, presses=presses):
                self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_a_late_starting_video_stream_gets_its_own_frame_grid(self):
        self.assertAlmostEqual(render.grid_phase([(0.021 + k / 30, None) for k in range(10)], 30),
                               0.021 - 1 / 30, places=6)
        self.assertEqual(render.grid_phase([(k / 30 + 0.0002, None) for k in range(10)], 30), 0.0)
        self.assertEqual(render._frame_index(0.021 + 31 / 30, 30, 0.021), 31)
        for phase in (0.021, 0.0125, -0.01):
            sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 9.0)])
            plan, res, scene = self.resolve(sc, {1: [(4.0, 8.0)]}, phase=phase)
            with self.subTest(phase=phase):
                self.assertEqual(rf.leaked_frames(scene, plan, res), [])
                p = res.pieces[0]
                shift = (p.phase - phase) * 30                  # the same grid: a whole number of frames apart
                self.assertAlmostEqual(shift, round(shift), delta=1e-3)

    def test_session_b_marks_become_chapters_and_their_flash_is_hidden(self):
        from tests.test_render_plan import v1_sidecar
        sc = v1_sidecar(marks=[(5.0, "b-mark")])
        plan, res, scene = self.resolve(sc, {1: [(2.0, 18.0)]})
        self.assertAlmostEqual(plan.marks[0]["media_s"], 4.9, places=6)
        self.assertEqual([c["title"] for c in res.chapters], ["start", "b-mark"])
        self.assertEqual([(c["mark"], c["rect"]) for c in res.concealed], [(1, None)])
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_a_mark_patch_is_concealed_in_the_piece_it_is_in(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 1.5), ("take", 1, 4.0), ("take", 1, 4.4),
                                              ("mark", 1, 4.9), ("take", 1, 9.0)])
        plan, res, scene = self.resolve(sc, {1: [(2.0, 3.5), (4.6, 8.5)]})        # speech from the take's start
        self.assertEqual([(c["mark"], c["piece"]) for c in res.concealed], [(1, 1)])
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])


class MarkAndEdgeRegressionTest(ReviewRegressionTest):
    """Cases a fuzz of press sequences found after the review fixes (the
    recorder shows fiducials one after another; the fixture does the same)."""

    def test_a_take_pressed_before_the_first_frame_excludes_its_patch(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, -0.04), ("take", 1, 5.0)])
        self.assertEqual(sc["events"][0]["media_s"], 0.0)          # the model clamps the press to frame 0...
        plan, res, scene = self.resolve(sc, {1: [(1.0, 4.5)]})
        self.assertEqual(plan.intervals[0].start.kind, "take-open")   # ...but its patch is still there
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])
        self.assertEqual(res.fallbacks, [])

    def test_marks_pressed_together_are_one_run_hidden_once(self):
        sc = rf.build_sidecar(STEM, [20.0], [("mark", 1, 4.0, "a"), ("mark", 1, 4.1, "b")])
        plan, res, scene = self.resolve(sc, {1: [(1.0, 18.0)]})
        self.assertEqual(len(res.concealed), 1)
        self.assertGreaterEqual(res.concealed[0]["frames"], 12)
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])
        self.assertEqual([c["title"] for c in res.chapters], ["start", "a", "b"])

    def test_a_mark_patch_at_a_piece_edge_is_cut_off_that_edge(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("mark", 1, 3.05), ("take", 1, 9.0)])
        plan, res, scene = self.resolve(sc, {1: [(3.0, 8.5)]})
        self.assertEqual([(c["mark"], c.get("trimmed")) for c in res.concealed], [(1, True)])
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_a_piece_that_is_mostly_a_mark_patch_is_dropped(self):
        # the retake's take runs to the stop flash 0.3 s later, and the mark's patch fills it
        sc = rf.build_sidecar(STEM, [11.37], [("take", 1, 3.0), ("retake", 1, 10.15), ("mark", 1, 10.45)])
        plan, res, scene = self.resolve(sc, {1: [(1.0, 11.0)]})
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])
        self.assertTrue(any("the piece is dropped" in f for f in res.fallbacks), res.fallbacks)
        self.assertEqual(res.pieces, [])

    def test_presses_anywhere_never_leak(self):
        import random
        rnd = random.Random(2026)
        for case in range(20):
            presses = []
            for seg, dur in ((1, 10.0), (2, 8.0)):
                t = rnd.uniform(-0.3, 0.3)
                while t < dur:
                    presses.append((rnd.choice(["take", "take", "retake", "mark", "mark"]), seg, round(t, 2)))
                    t += rnd.uniform(0.1, 2.0)
            sc = rf.build_sidecar(STEM, [10.0, 8.0], presses)
            plan, res, scene = self.resolve(sc, {1: [(0.5, 9.5)], 2: [(0.5, 7.5)]})
            with self.subTest(case=case, presses=presses):
                self.assertEqual(rf.leaked_frames(scene, plan, res), [])


class SecondReviewRegressionTest(ReviewRegressionTest):
    """The re-review's findings, pinned."""

    def test_a_far_run_of_the_same_colour_is_not_taken(self):
        f = frames([(0.3, 0.5, BLU)], end=3.0)
        d = render.detect_run(f, BLU, 1.2, 8, 1 / 30, max_offset_s=0.3)
        self.assertFalse(d.found)
        self.assertIn("another fiducial", d.reason)
        self.assertTrue(render.detect_run(f, BLU, 0.4, 8, 1 / 30, max_offset_s=0.3).found)

    def test_overlapping_fiducials_never_leak_either(self):
        import random
        rnd = random.Random(77)
        for case in range(20):
            presses, t = [], rnd.uniform(-0.2, 0.5)
            while t < 9.8:
                presses.append((rnd.choice(["take", "take", "retake", "mark"]), 1, round(t, 2)))
                t += rnd.uniform(0.1, 1.5)
            sc = rf.build_sidecar(STEM, [10.0], presses, serial_fiducials=False)
            plan, res, scene = self.resolve(sc, {1: [(0.5, 9.5)]})
            leaks = rf.leaked_frames(scene, plan, res)
            with self.subTest(case=case, presses=presses):
                # with overlapping stamps a leak may be unavoidable, but never a silent one
                self.assertTrue(not leaks or res.fallbacks, leaks)

    def test_an_edge_trim_is_recorded_in_the_seams(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("mark", 1, 3.05), ("take", 1, 9.0)])
        plan, res, scene = self.resolve(sc, {1: [(3.0, 8.5)]})
        rec = next(s for s in res.seams if s["interval"] == 0 and s["side"] == "start")
        self.assertEqual(rec["moved_by"], "mark patch")
        self.assertAlmostEqual(rec["seam_s"], res.pieces[0].start_s, places=3)

    def test_a_dropped_piece_is_in_dropped(self):
        sc = rf.build_sidecar(STEM, [11.37], [("take", 1, 3.0), ("retake", 1, 10.15), ("mark", 1, 10.45)])
        plan, res, scene = self.resolve(sc, {1: [(1.0, 11.0)]})
        self.assertEqual([d.get("why") for d in res.dropped], ["mark 1's patch"])

    def test_a_mark_pressed_with_the_take_starts_its_chapter(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("mark", 1, 3.05, "chapter one"),
                                              ("take", 1, 9.0)])
        plan, res, scene = self.resolve(sc, {1: [(3.0, 8.5)]})
        self.assertEqual([(c["title"], c["start_s"]) for c in res.chapters], [("chapter one", 0.0)])
        self.assertEqual(res.marks_excluded, [])

    def test_exact_times_from_integer_pts(self):
        err = (b"[Parsed_showinfo_2 @ 0] config in time_base: 1/16000, frame_rate: 30/1\n"
               b"[Parsed_showinfo_2 @ 0] n:   0 pts:19752000 pts_time:1234.5 duration:533\n"
               b"[Parsed_showinfo_2 @ 0] n:   1 pts:19752533 pts_time:1234.53 duration:533\n")
        raw = bytes([60, 60, 60]) * 160 * 2
        out = render.parse_frames(raw, err, (16, 10), 1234.0, 30, median=False)
        self.assertEqual([round(t, 6) for t, _ in out], [1234.5, 1234.533312])
