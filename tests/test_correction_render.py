"""Session C1c in the render: no fiducial colour reaches the cut for any press
sequence of the correction chart. A close that a correction moved later
leaves its red patch inside the kept take, and any correction patch that
lands in kept material is likewise not a kept edge: both are concealed like
mark patches. The fuzzer of C1b is extended to the chart's sequences (debounce,
pauses, presses while paused, ① ② ③). And sidecars written under C1a's retake
rule still render exactly as recorded.

No process anywhere except the one end-to-end render through fake_ffmpeg."""

import copy
import json
import os
import random
import unittest

from peep import catalog, config as c, render
from tests import render_fixtures as rf
from tests.test_render_run import RunHarness

STEM = "2026-10-07-demo"
CFG = c.RenderConfig()


def resolve(sc, speech=None, **scene_kw):
    plan = render.build_plan(sc, stem=STEM)
    scene = rf.scene_for(sc, speech, **scene_kw)
    res = render.resolve(plan, rf.SceneAnalyzer(scene), CFG)
    return plan, res, scene


def kept(plan) -> list:
    return [(iv.segment, iv.start_s, iv.end_s, iv.take) for iv in plan.intervals]


class ChartSequencesTest(unittest.TestCase):
    SPEECH = {1: [(1.0, 19.0)], 2: [(1.0, 14.0)]}

    def test_1_a_moved_close_conceals_the_superseded_red_patch(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 6.0), ("correct", 1, 9.0),
                                             ("take", 1, 12.0), ("take", 1, 15.0)])
        plan, res, scene = resolve(sc, self.SPEECH)
        self.assertEqual(kept(plan), [(1, 3.0, 9.0, 1), (1, 12.0, 15.0, 2)])
        self.assertEqual(plan.intervals[0].end.fiducial.color, "#FFFF00")      # the yellow patch is the edge now
        [stray] = plan.patches
        self.assertEqual((stray["event"], stray["what"]), (2, "the superseded close patch of event 2"))
        [hidden] = res.concealed
        self.assertEqual((hidden["event"], hidden["mark"], hidden["piece"]), (2, None, 0))
        self.assertIsNotNone(hidden["rect"])
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])
        self.assertEqual(res.fallbacks, [])

    def test_1_2_the_whole_take_drops_and_a_fresh_one_opens(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 6.0), ("correct", 1, 9.0),
                                             ("correct", 1, 12.0), ("take", 1, 16.0)])
        plan, res, scene = resolve(sc, self.SPEECH)
        self.assertEqual(kept(plan), [(1, 12.0, 16.0, 2)])
        self.assertEqual(plan.intervals[0].start.fiducial.color, "#FFFF00")
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])
        self.assertEqual(res.concealed, [])                    # every stray patch is in cut material

    def test_1_2_3_flubbed_again(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 6.0), ("correct", 1, 9.0),
                                             ("correct", 1, 12.0), ("correct", 1, 14.0), ("take", 1, 18.0)])
        plan, res, scene = resolve(sc, self.SPEECH)
        self.assertEqual(kept(plan), [(1, 14.0, 18.0, 3)])
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_a_close_moved_across_a_pause_spans_it_and_its_old_patch_is_hidden(self):
        sc = rf.build_sidecar(STEM, [20.0, 15.0], [("take", 1, 3.0), ("take", 1, 6.0), ("correct", 2, 4.0)])
        plan, res, scene = resolve(sc, self.SPEECH)
        self.assertEqual(kept(plan), [(1, 3.0, 20.0, 1), (2, 0.0, 4.0, 1)])
        self.assertEqual([x["event"] for x in res.concealed], [2])
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_corrections_while_paused_move_the_close_to_the_pause(self):
        sc = rf.build_sidecar(STEM, [20.0, 15.0], [("take", 1, 3.0), ("take", 1, 6.0), ("correct", "paused", 1)])
        plan, res, scene = resolve(sc, self.SPEECH)
        self.assertEqual(kept(plan), [(1, 3.0, 20.0, 1)])
        self.assertEqual(plan.intervals[0].end.kind, "segment-end")             # cut at the stop flash
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_presses_made_while_resuming_never_leak(self):
        """Pressed after the resume, before the next segment's first frame: the model
        places them in the pause, the recorder shows their patch in the new segment,
        after its start flash. The plan finds each where it was shown (C1c review)."""
        cases = {
            "a correction drops the open take; the fresh one opens there":
                ([("take", 1, 3.0), ("correct", "resuming", 1), ("take", 2, 8.0)], [(2, 0.0, 8.0, 2)]),
            "a close, then moved later in the next segment":
                ([("take", 1, 3.0), ("take", "resuming", 1), ("correct", 2, 6.0)],
                 [(1, 3.0, 20.0, 1), (2, 0.0, 6.0, 1)]),
            "a take opened while resuming":
                ([("take", "resuming", 1), ("take", 2, 8.0)], [(2, 0.0, 8.0, 1)]),
            "a mark while resuming, inside a take":
                ([("take", 1, 3.0), ("mark", "resuming", 1, "back"), ("take", 2, 8.0)],
                 [(1, 3.0, 20.0, 1), (2, 0.0, 8.0, 1)]),
            "a mark held over the pause, with no take":
                ([("mark", "held", 1, "late")], [(1, 0.0, 20.0, None), (2, 0.0, 15.0, None)]),
            "a take held over the pause":
                ([("take", "held", 1), ("take", 2, 8.0)], [(1, 19.7, 20.0, 1), (2, 0.0, 8.0, 1)]),
            "a close held over the pause, then moved later":
                ([("take", 1, 3.0), ("take", "held", 1), ("correct", 2, 6.0)],
                 [(1, 3.0, 20.0, 1), (2, 0.0, 6.0, 1)]),
        }
        for rule in ("correction", "c1a"):
            for name, (presses, want) in cases.items():
                if rule == "c1a" and any(p[0] == "correct" for p in presses):
                    continue                                   # C1a had no such key: its retake rule differs
                with self.subTest(rule=rule, case=name):
                    sc = rf.build_sidecar(STEM, [20.0, 15.0], presses, rule=rule)
                    plan, res, scene = resolve(sc, self.SPEECH)
                    self.assertEqual(kept(plan), want)
                    self.assertEqual(rf.leaked_frames(scene, plan, res), [])
                    moved = [e for e in sc["events"] if e.get("fiducial") and e.get("segment") != 2
                             and e["fiducial"]["shown_qpc"] > sc["segments"][1]["video_epoch_qpc_est"]]
                    self.assertTrue(moved)                      # the case really has a press shown in segment 2
                    self.assertFalse([f for f in res.fallbacks if "not found" in f], res.fallbacks)
        sc = rf.build_sidecar(STEM, [20.0, 15.0], [("take", 1, 3.0), ("correct", "resuming", 1), ("take", 2, 8.0)])
        start = render.build_plan(sc, stem=STEM).intervals[0].start
        self.assertEqual((start.kind, start.fiducial.color), ("take-open", "#FFFF00"))   # its own patch, not the flash

    def test_an_in_kept_correction_patch_is_concealed(self):
        """A yellow patch inside kept material that is no kept edge's fiducial
        (any sequence that leaves one there) is hidden like the others."""
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 15.0)])
        seg = sc["segments"][0]
        e = seg["video_epoch_qpc_est"]
        stray = copy.deepcopy(sc["events"][0])
        stray.update(id=3, kind="correct", action="moved-close", take=1, media_s=8.0, qpc=round(e + 8.0, 6))
        stray["fiducial"] = {**rf._flash(rf.YELLOW, e + 8.05, seg["ffmpeg_started_qpc"]), "style": "patch",
                             "rect": dict(rf.RECT), "screen": dict(rf.SCREEN), "kind": "correct-moved-close"}
        sc["events"].append(stray)
        plan, res, scene = resolve(sc, self.SPEECH)
        self.assertEqual([p["event"] for p in plan.patches], [3])
        self.assertEqual([(x["event"], x["piece"]) for x in res.concealed], [(3, 0)])
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_a_mark_inside_a_dropped_take_is_dropped_with_it(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("mark", 1, 5.0, "lost"), ("correct", 1, 8.0),
                                             ("take", 1, 12.0)])
        plan, res, scene = resolve(sc, self.SPEECH)
        self.assertEqual(kept(plan), [(1, 8.0, 12.0, 2)])
        self.assertEqual([m["label"] for m in res.marks_excluded], ["lost"])
        self.assertEqual(res.chapters, [])
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_dropping_every_take_renders_nothing(self):
        sc = rf.build_sidecar(STEM, [10.0, 8.0], [("take", 1, 3.0), ("correct", "paused", 1)])
        sc["segments"] = sc["segments"][:1]                  # the recording ended in that pause
        sc["summary"] = {**sc["summary"], "kept": [k for k in sc["summary"]["kept"] if k["segment"] == 1]}
        self.assertEqual(sc["summary"]["kept"], [])
        self.assertFalse(render.should_render("always", sc["summary"]))
        self.assertEqual(render.build_plan(sc, stem=STEM).intervals, [])


class ChartFuzzTest(unittest.TestCase):
    """C1b's fuzz invariant, over the chart: random take / correct / mark presses
    (hotkeys, so the debounce applies), across one to three segments, with
    presses while paused, at and before the flashes."""

    def presses(self, rnd, durations):
        out = []
        for seg, dur in enumerate(durations, 1):
            t = rnd.uniform(-0.3, 1.2)
            while t < dur:
                out.append((rnd.choice(["take", "take", "correct", "correct", "mark"]), seg, round(t, 2)))
                t += rnd.choice([rnd.uniform(0.1, 1.2), rnd.uniform(1.0, 3.0)])
            if seg < len(durations) and rnd.random() < 0.5:
                out += [(rnd.choice(["take", "correct"]), "paused", seg) for _ in range(rnd.randint(1, 2))]
            if seg < len(durations) and rnd.random() < 0.5:
                out += [(rnd.choice(["take", "correct", "mark"]), rnd.choice(["resuming", "held"]), seg)
                        for _ in range(rnd.randint(1, 2))]
        return out

    def test_no_chart_sequence_leaks_a_fiducial_frame(self):
        rnd = random.Random(20261007)
        moved = 0
        for case in range(60):
            durations = [round(rnd.uniform(8.0, 14.0), 2) for _ in range(rnd.randint(1, 3))]
            presses = self.presses(rnd, durations)
            sc = rf.build_sidecar(STEM, durations, presses, source="hotkey")
            moved += sum(1 for e in sc["events"] if e.get("action") == "moved-close")
            speech = {k: [(0.5, d - 0.5)] for k, d in enumerate(durations, 1)}
            plan, res, scene = resolve(sc, speech)
            with self.subTest(case=case, durations=durations, presses=presses):
                self.assertEqual(rf.leaked_frames(scene, plan, res), [])
        self.assertGreater(moved, 20)                            # the fuzz really exercises ①

    def test_overlapping_chart_fiducials_never_leak_silently(self):
        rnd = random.Random(77)
        for case in range(25):
            presses, t = [], rnd.uniform(-0.2, 0.5)
            while t < 9.8:
                presses.append((rnd.choice(["take", "take", "correct", "mark"]), 1, round(t, 2)))
                t += rnd.uniform(0.1, 1.5)
            sc = rf.build_sidecar(STEM, [10.0], presses, serial_fiducials=False)
            plan, res, scene = resolve(sc, {1: [(0.5, 9.5)]})
            leaks = rf.leaked_frames(scene, plan, res)
            with self.subTest(case=case, presses=presses):
                self.assertTrue(not leaks or res.fallbacks, leaks)


class C1aSidecarsTest(unittest.TestCase):
    """Recordings made under C1a's retake rule render as recorded: the render
    reads their resolved boundaries (takes, summary.kept), never the press
    semantics, so nothing needed migrating."""

    def test_they_render_their_recorded_intervals_and_leak_nothing(self):
        rnd = random.Random(2026)
        differ = 0
        for case in range(30):
            presses = []
            for seg, dur in ((1, 10.0), (2, 8.0)):
                t = rnd.uniform(-0.3, 0.3)
                while t < dur:
                    presses.append((rnd.choice(["take", "take", "retake", "mark"]), seg, round(t, 2)))
                    t += rnd.uniform(0.3, 2.0)
            if rnd.random() < 0.5:
                presses.append((rnd.choice(["take", "retake", "mark"]), rnd.choice(["resuming", "held"]), 1))
            old = rf.build_sidecar(STEM, [10.0, 8.0], presses, rule="c1a")
            old = json.loads(json.dumps(old))                      # as read back from disk
            self.assertNotIn("take_rule", old)
            plan, res, scene = resolve(old, {1: [(0.5, 9.5)], 2: [(0.5, 7.5)]})
            recorded = [(k["segment"], k["start_s"], k["end_s"], k["take"]) for k in old["summary"]["kept"]]
            with self.subTest(case=case, presses=presses):
                self.assertEqual(kept(plan), recorded)
                self.assertEqual(rf.leaked_frames(scene, plan, res), [])
            new = rf.build_sidecar(STEM, [10.0, 8.0], presses)
            differ += new["summary"]["kept"] != old["summary"]["kept"]
        self.assertGreater(differ, 0)                              # the rule did change; old files keep theirs

    def test_a_c1a_retake_record_is_read_as_it_was_written(self):
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("take", 1, 8.0), ("retake", 1, 12.0),
                                             ("take", 1, 20.0)], rule="c1a")
        retake = sc["events"][2]
        self.assertEqual((retake["kind"], retake["action"], retake["discarded_take"]), ("retake", "open", 1))
        # under C1a the retake after a close redid the take: take 1 discarded, take 2 from 12 s
        self.assertEqual(kept(render.build_plan(sc, stem=STEM)), [(1, 12.0, 20.0, 2)])
        # the same presses under C1c's chart move the close instead (and the take press opens take 2)
        new = rf.build_sidecar(STEM, [30.0], [("take", 1, 3.0), ("take", 1, 8.0), ("retake", 1, 12.0),
                                              ("take", 1, 20.0)])
        self.assertEqual(kept(render.build_plan(new, stem=STEM)), [(1, 3.0, 12.0, 1), (1, 20.0, 30.0, 2)])


class EndToEndTest(RunHarness, unittest.TestCase):
    def test_a_moved_close_renders_with_its_old_patch_concealed(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 6.0), ("correct", 1, 9.0),
                                             ("take", 1, 12.0), ("take", 1, 15.0)])
        self.record(sc, {1: [(1.0, 19.0)]})
        res = render.Renderer(self.cfg).render("last")
        self.assertTrue(res.ok, res.message)
        block = self.sidecar()["render"]
        self.assertEqual(block["status"], "ok")
        self.assertEqual([(x["event"], x["what"]) for x in block["concealed"]],
                         [(2, "the superseded close patch of event 2")])
        self.assertEqual([b["color"] for b in block["boundaries"]], ["#0000FF", "#FFFF00", "#0000FF", "#FF0000"])
        self.assertEqual(block["fallbacks"], [])
        self.assertEqual(self.sidecar()["take_rule"], "correction/1")
        self.assertTrue(os.path.exists(self.folder / f"{STEM}.cut.mp4"))
        self.assertEqual(catalog.read_sidecar(self.folder / f"{STEM}.json")["render"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
