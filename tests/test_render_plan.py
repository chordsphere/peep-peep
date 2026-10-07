"""Session C1b: the render plan as a pure function of the sidecar. Every
event shape C1a can record (zero presses, takes, retakes, takes across a
pause, presses while paused, a take left open, everything discarded,
failed segments, a missing summary) and both sidecar versions (`/1`
through catalog.as_v2, with and without A.1's QPC stamps)."""

import copy
import unittest

from tests import render_fixtures as rf
from peep import catalog, render

STEM = "2026-10-07-demo"


def plan_of(sc):
    return render.build_plan(sc, stem=STEM)


def v1_sidecar(duration=20.0, qpc=True, marks=()):
    """A peep.sidecar/1 as A.1 (qpc=True) or A (qpc=False) wrote it."""
    sc = catalog.new_sidecar(uid="u1", slug="demo", collection="inbox", file=f"{STEM}.mp4", created="t")
    sc["schema"] = catalog.SIDECAR_SCHEMA_V1
    for k in ("segments", "events", "takes", "pauses", "summary"):
        sc.pop(k)
    e, launch = 500.0, 499.9
    sc.update(status="ok", duration_s=duration)
    sc["video"].update(pipeline="qsv", fps=30, capture_size="2560x1600", output_size="2560x1600")
    start = {"color": "#FF00FF", "duration_ms": 200, "shown": True, "since_ffmpeg_start_s": 0.86, "actual_ms": 201}
    stop = {"color": "#00FF00", "duration_ms": 200, "shown": True, "since_ffmpeg_start_s": duration - 0.6,
            "actual_ms": 199}
    if qpc:
        start["shown_qpc"], stop["shown_qpc"] = e + 0.76, e + duration - 0.7
        sc["timeline"].update(ffmpeg_started_qpc=launch, input0_qpc=e + 0.058)
        sc["audio"] = {"device": None, "codec": "aac", "bitrate": "160k", "offset_ms": 0, "sources": "system",
                       "tracks": [{"index": 0, "content": "system"}],
                       "epoch": {"video_epoch_qpc_est": e, "ffmpeg_started_qpc": launch},
                       "clap": {"played": True, "requested_qpc": e + 0.755}}
    else:
        sc["audio"] = {"device": "Microphone Array", "codec": "aac", "bitrate": "160k", "offset_ms": 0}
    sc["flash"] = {"enabled": True, "start": start, "stop": stop}
    sc["marks"] = [{"t": t, "label": lab, "source": "hotkey",
                    "flash": {"color": "#00FFFF", "duration_ms": 200, "shown": True, "since_ffmpeg_start_s": t,
                              "actual_ms": 200}} for t, lab in marks]
    return sc


class ZeroPressTest(unittest.TestCase):
    def test_one_segment_renders_whole_trimmed_to_its_flashes(self):
        p = plan_of(rf.build_sidecar(STEM, [20.0]))
        self.assertTrue(p.whole)
        self.assertEqual(len(p.intervals), 1)
        iv = p.intervals[0]
        self.assertEqual((iv.segment, iv.start_s, iv.end_s, iv.take), (1, 0.0, 20.0, None))
        self.assertEqual((iv.start.kind, iv.end.kind), ("segment-start", "segment-end"))
        self.assertEqual((iv.start.fiducial.color, iv.end.fiducial.color), ("#FF00FF", "#00FF00"))
        self.assertAlmostEqual(iv.start.fiducial.stamp_s, rf.START_FLASH_AT, places=6)
        self.assertAlmostEqual(iv.end.fiducial.stamp_s, 20.0 - rf.STOP_FLASH_BEFORE, places=6)
        self.assertEqual(iv.start.fiducial.stamp_quality, "qpc")
        # the zero-press rule: the outer edges are trimmed to the flashes only, never for silence
        self.assertFalse(iv.start.adjustable or iv.end.adjustable)
        self.assertEqual((p.tracks, p.fps, p.size, p.pipeline), (["system"], 30, (2560, 1600), "qsv"))

    def test_paused_but_no_take_joins_every_segment_and_only_the_joins_move(self):
        p = plan_of(rf.build_sidecar(STEM, [10.0, 8.0, 6.0]))
        self.assertTrue(p.whole)
        self.assertEqual([(i.segment, i.start_s, i.end_s) for i in p.intervals],
                         [(1, 0.0, 10.0), (2, 0.0, 8.0), (3, 0.0, 6.0)])
        self.assertEqual([(i.start.adjustable, i.end.adjustable) for i in p.intervals],
                         [(False, True), (True, True), (True, False)])
        self.assertTrue(all(i.start.kind == "segment-start" and i.end.kind == "segment-end" for i in p.intervals))

    def test_no_flash_recording_has_nothing_to_find(self):
        p = plan_of(rf.build_sidecar(STEM, [10.0], flash=False))
        self.assertEqual(p.intervals[0].start.fiducial.style, "none")
        self.assertEqual(p.intervals[0].end.fiducial.style, "none")

    def test_no_audio(self):
        self.assertEqual(plan_of(rf.build_sidecar(STEM, [10.0], tracks=())).tracks, [])

    def test_separate_tracks_layout(self):
        p = plan_of(rf.build_sidecar(STEM, [10.0], tracks=("system", "mic")))
        self.assertEqual(p.tracks, ["system", "mic"])


class TakesTest(unittest.TestCase):
    def test_takes_become_intervals_with_their_patches(self):
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("take", 1, 9.0), ("take", 1, 12.0),
                                              ("take", 1, 20.0)])
        p = plan_of(sc)
        self.assertFalse(p.whole)
        self.assertEqual([(i.start_s, i.end_s, i.take) for i in p.intervals], [(3.0, 9.0, 1), (12.0, 20.0, 2)])
        first = p.intervals[0]
        self.assertEqual((first.start.kind, first.start.fiducial.color, first.start.fiducial.style),
                         ("take-open", "#0000FF", "patch"))
        self.assertEqual((first.end.kind, first.end.fiducial.color), ("take-close", "#FF0000"))
        self.assertAlmostEqual(first.start.fiducial.stamp_s, 3.05, places=6)     # the patch went up after the press
        self.assertEqual(first.start.fiducial.rect, rf.RECT)
        self.assertTrue(all(i.start.adjustable and i.end.adjustable for i in p.intervals))
        self.assertEqual(first.start.event, sc["takes"][0]["open"]["event"])

    def test_retake_discards_and_its_yellow_patch_opens_the_kept_take(self):
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("retake", 1, 8.0), ("take", 1, 15.0),
                                              ("retake", 1, 18.0), ("take", 1, 25.0)])
        p = plan_of(sc)
        self.assertEqual(sc["summary"]["takes_discarded"], 2)
        self.assertEqual([(i.start_s, i.end_s, i.take) for i in p.intervals], [(18.0, 25.0, 3)])
        self.assertEqual(p.intervals[0].start.fiducial.color, "#FFFF00")

    def test_a_take_across_a_pause_is_cut_at_the_flashes(self):
        sc = rf.build_sidecar(STEM, [20.0, 15.0], [("take", 1, 5.0), ("take", 2, 6.0)])
        p = plan_of(sc)
        self.assertEqual([(i.segment, i.start_s, i.end_s) for i in p.intervals], [(1, 5.0, 20.0), (2, 0.0, 6.0)])
        a, b = p.intervals
        self.assertEqual((a.start.kind, a.end.kind, b.start.kind, b.end.kind),
                         ("take-open", "segment-end", "segment-start", "take-close"))
        self.assertEqual((a.end.fiducial.color, b.start.fiducial.color), ("#00FF00", "#FF00FF"))
        self.assertAlmostEqual(b.start.fiducial.stamp_s, rf.START_FLASH_AT, places=6)   # segment 2's own epoch

    def test_a_take_opened_while_paused_starts_with_the_next_segment(self):
        sc = rf.build_sidecar(STEM, [10.0, 12.0], [("take", "paused", 1), ("take", 2, 8.0)])
        p = plan_of(sc)
        self.assertEqual([(i.segment, i.start_s, i.end_s) for i in p.intervals], [(2, 0.0, 8.0)])
        self.assertEqual(p.intervals[0].start.kind, "segment-start")
        self.assertTrue(p.intervals[0].start.adjustable)          # a take's edge, even at a flash

    def test_a_take_left_open_ends_with_the_recording(self):
        p = plan_of(rf.build_sidecar(STEM, [10.0, 12.0], [("take", 1, 4.0)]))
        self.assertEqual([(i.segment, i.start_s, i.end_s) for i in p.intervals], [(1, 4.0, 10.0), (2, 0.0, 12.0)])
        self.assertEqual(p.intervals[-1].end.kind, "segment-end")

    def test_every_take_discarded_leaves_nothing(self):
        p = plan_of(rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 6.0), ("retake", 1, 9.0),
                                                     ("retake", 1, 12.0)]))
        # the second retake discards the take the first opened, and opens one that runs to the end
        self.assertEqual([(i.start_s, i.end_s) for i in p.intervals], [(12.0, 20.0)])
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 6.0)])
        sc["takes"][0].update(discarded_by=99, status="discarded")
        sc["summary"]["kept"] = []
        self.assertEqual(plan_of(sc).intervals, [])

    def test_a_press_with_no_patch_shown_is_a_bare_boundary(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 9.0)])
        for ev in sc["events"]:
            ev["fiducial"] = None
        iv = plan_of(sc).intervals[0]
        self.assertEqual((iv.start.fiducial.style, iv.start.fiducial.stamp_s, iv.start.fiducial.stamp_quality),
                         ("none", 3.0, "press"))


class RobustnessTest(unittest.TestCase):
    def test_failed_later_segment_is_left_out_and_said(self):
        sc = rf.build_sidecar(STEM, [10.0, 5.0], status="failed")
        sc["segments"][1]["file"] = f"{STEM}.seg2.mkv"
        p = plan_of(sc)
        self.assertEqual([s.index for s in p.segments], [1])
        self.assertEqual([i.segment for i in p.intervals], [1])
        self.assertIn("segment 2 did not finish cleanly", p.notes[0])

    def test_missing_summary_is_derived_from_the_takes(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 9.0)])
        sc["summary"] = None
        p = plan_of(sc)
        self.assertEqual([(i.start_s, i.end_s) for i in p.intervals], [(3.0, 9.0)])
        self.assertIn("derived from its takes", p.notes[0])

    def test_digest_ignores_file_names_but_not_events(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 9.0)])
        d = render.source_digest(catalog.as_v2(sc))
        renamed = copy.deepcopy(sc)
        for s in renamed["segments"]:
            s["file"] = s["file"].replace("demo", "renamed")
        renamed["file"] = "x.mp4"
        self.assertEqual(render.source_digest(catalog.as_v2(renamed)), d)
        changed = copy.deepcopy(sc)
        changed["events"][0]["media_s"] = 3.5
        self.assertNotEqual(render.source_digest(catalog.as_v2(changed)), d)

    def test_marks_carry_their_patch_and_pause_marks_are_flagged(self):
        sc = rf.build_sidecar(STEM, [10.0, 10.0], [("mark", 1, 4.0, "intro"), ("mark", "paused", 1, "break")])
        p = plan_of(sc)
        self.assertEqual([(m["segment"], m["media_s"], m["label"], m["after"]) for m in p.marks],
                         [(1, 4.0, "intro", False), (1, None, "break", True)])
        self.assertEqual((p.marks[0]["fiducial"].color, p.marks[0]["fiducial"].style), ("#00FFFF", "patch"))
        self.assertEqual(p.marks[1]["fiducial"].style, "none")


class V1SidecarTest(unittest.TestCase):
    def test_a1_sidecar_renders_whole_with_qpc_stamps(self):
        p = render.build_plan(v1_sidecar(), stem=STEM)
        self.assertTrue(p.whole)
        iv = p.intervals[0]
        self.assertEqual((iv.start_s, iv.end_s), (0.0, 20.0))
        self.assertAlmostEqual(iv.start.fiducial.stamp_s, 0.76, places=6)
        self.assertEqual(iv.start.fiducial.stamp_quality, "qpc")
        self.assertAlmostEqual(p.segments[0].clap["requested_s"], 0.755, places=6)
        self.assertEqual(p.tracks, ["system"])

    def test_a_era_sidecar_has_approximate_stamps_and_one_track(self):
        p = render.build_plan(v1_sidecar(qpc=False, marks=[(5.0, "b-mark")]), stem=STEM)
        f = p.intervals[0].start.fiducial
        self.assertEqual(f.stamp_quality, "approximate")
        self.assertAlmostEqual(f.stamp_s, 0.86 - render.APPROX_LAUNCH_TO_FRAME_S, places=6)
        self.assertEqual(p.tracks, ["audio"])
        self.assertEqual((p.marks[0]["label"], p.marks[0]["fiducial"].style), ("b-mark", "full"))

    def test_media_time_prefers_qpc_then_the_measured_launch(self):
        self.assertEqual(render.media_time(10.5, 0.9, 10.0, 9.9), (0.5, "qpc"))
        t, q = render.media_time(None, 0.9, 10.0, 9.9)
        self.assertAlmostEqual(t, 0.8)
        self.assertEqual(q, "launch")
        self.assertEqual(render.media_time(None, None, 10.0, 9.9), (None, "none"))


class ShouldRenderTest(unittest.TestCase):
    def test_policy(self):
        whole = {"whole": True, "segments": 1}
        self.assertTrue(render.should_render("always", whole))
        self.assertFalse(render.should_render("never", {"whole": False}))
        self.assertFalse(render.should_render("takes", whole))
        self.assertTrue(render.should_render("takes", {"whole": False, "segments": 1}))
        self.assertTrue(render.should_render("takes", {"whole": True, "segments": 2}))
        self.assertFalse(render.should_render("takes", None))


if __name__ == "__main__":
    unittest.main()


class NothingKeptPolicyTest(unittest.TestCase):
    def test_nothing_kept_is_never_rendered(self):
        self.assertFalse(render.should_render("always", {"whole": False, "kept": []}))
        self.assertTrue(render.should_render("always", {"whole": False, "kept": [{"segment": 1}]}))
