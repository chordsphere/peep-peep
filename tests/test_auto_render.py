"""Session C1d: the render of learned-screen boundaries, and the promise that
recordings which never learn a screen cut exactly as before.

  NeverLearnedTest     120 random recordings (both take rules, presses anywhere, pauses,
                       held and resuming presses, 0-2 audio tracks): their kept intervals,
                       pieces, boundaries, seams, concealments, chapters and fallbacks
                       hash to the value the code before C1d produced (computed on the
                       unmodified tree), and so does every cut's source digest
  RefineTest           an end screen's first frame, a start screen's last, exactly; a
                       screen learned long after it appeared followed back; fallbacks
                       loud and erring toward cutting more
  FuzzTest             random screens, learns, forgets, takes, corrections and marks
                       through the real hysteresis, model, plan and render: no fiducial
                       frame and no frame of a screen that made an edge is ever cut in
  EndToEndTest         Renderer through fake_ffmpeg (a separate process): the thumbnail
                       decode, the sidecar's render block, --dry-run's description
"""

import hashlib
import json
import math
import os
import random
import unittest

from peep import config as c, render
from tests import render_fixtures as rf
from tests.test_render_run import RunHarness

CFG = c.RenderConfig()
STEM = "2026-10-08-auto"

# The value tests/test_auto_render.py's never_learned_digest() gave on the tree before C1d
# (git 5717899, C1c applied), and gives now. The computation is below; validation.sh also
# checks it against the pre-change modules when they are reachable.
NEVER_LEARNED_DIGEST = "d0356dc8e2fceb43387f1b449db9fc947b9076bb5717a2624394c43f7422abef"
NEVER_LEARNED_SOURCE_DIGEST = "81500a7d9e107133120bd64d4f8b692a83c7395b3105249031e5687794f7a2f9"


def never_learned_cases():
    rnd = random.Random(20261008)
    for _ in range(120):
        nseg = rnd.randint(1, 3)
        durs = [round(rnd.uniform(6.0, 14.0), 2) for _ in range(nseg)]
        presses = []
        for k, d in enumerate(durs, 1):
            t = rnd.uniform(-0.3, 1.0)
            while t < d - 0.2:
                presses.append((rnd.choice(["take", "take", "take", "correct", "mark"]), k, round(t, 2)))
                t += rnd.uniform(0.3, 3.0)
            if k < nseg:
                for where in ("paused", "resuming", "held"):
                    if rnd.random() < 0.25:
                        presses.append((rnd.choice(["take", "correct", "mark"]), where, k))
        rule = rnd.choice(["correction", "correction", "c1a"])
        source = rnd.choice(["cli", "hotkey"])
        tracks = rnd.choice([(), ("system",), ("system", "mic")])
        speech = {k: [(0.5, d - 0.5)] for k, d in enumerate(durs, 1)} if rnd.random() < 0.6 else None
        yield durs, presses, rule, source, tracks, speech


def never_learned_digest() -> tuple[str, str]:
    """(cut digest, source digest) over never_learned_cases(). Uses only what the
    fixtures and the render offered before C1d, so the same function runs on the
    old tree."""
    out, src = [], hashlib.sha256()
    for durs, presses, rule, source, tracks, speech in never_learned_cases():
        sc = rf.build_sidecar("2026-10-08-never-learned", durs, presses, rule=rule, source=source, tracks=tracks)
        src.update(render.source_digest(sc).encode())
        plan = render.build_plan(sc, stem="x")
        res = render.resolve(plan, rf.SceneAnalyzer(rf.scene_for(sc, speech)), CFG)
        out.append({"kept": sc["summary"]["kept"],
                    "intervals": [[iv.segment, iv.start_s, iv.end_s, iv.take, iv.start.kind, iv.end.kind,
                                   iv.start.adjustable, iv.end.adjustable] for iv in plan.intervals],
                    "pieces": [[p.segment, p.take, p.k1, p.k2, p.phase] for p in res.pieces],
                    "boundaries": [[b.get("kind"), b.get("method"), b.get("bound_s")] for b in res.boundaries],
                    "seams": [[s.get("side"), s.get("seam_s"), s.get("how")] for s in res.seams],
                    "concealed": res.concealed, "chapters": res.chapters, "fallbacks": res.fallbacks,
                    "digest_takes": len(sc["takes"])})
    return hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest(), src.hexdigest()


class NeverLearnedTest(unittest.TestCase):
    def test_recordings_that_never_learn_a_screen_cut_exactly_as_before(self):
        cut, source = never_learned_digest()
        self.assertEqual(cut, NEVER_LEARNED_DIGEST)
        self.assertEqual(source, NEVER_LEARNED_SOURCE_DIGEST)        # every existing cut stays current

    def test_their_sidecars_have_no_auto_takes_block(self):
        sc = rf.build_sidecar(STEM, [10.0], [("take", 1, 2.0), ("take", 1, 6.0)])
        self.assertNotIn("auto_takes", sc)
        self.assertFalse(any(t.get("implicit") for t in sc["takes"]))


def run(sc, spans, cfg=CFG, fail=()):
    plan = render.build_plan(sc, stem="x")
    scene = rf.scene_for(sc, screen_spans=spans)
    an = rf.SceneAnalyzer(scene, fail=fail)
    return plan, render.resolve(plan, an, cfg), scene, an


def frame_at_or_after(t, fps=rf.FPS):
    return math.ceil(t * fps - 1e-6) / fps


class RefineTest(unittest.TestCase):
    def screen_bounds(self, res):
        return [b for b in res.boundaries if b.get("style") == "screen"]

    def test_learned_while_showing__followed_back_to_its_first_frame(self):
        spans = {1: [(10.0, 14.0, 7)]}
        sc = rf.build_sidecar(STEM, [40.0], [], tracks=(), screens={"spans": spans, "learn": [("end", 1, 13.5)]})
        plan, res, scene, _ = run(sc, spans)
        self.assertTrue(plan.intervals[0].start.kind == "segment-start" and not plan.intervals[0].start.adjustable)
        [b] = self.screen_bounds(res)
        self.assertEqual((b["method"], b["side"], b["from_learn"]), ("detected", "end", True))
        self.assertEqual(b["bound_s"], 10.0)                                 # learned at 13.5, cut at 10.0
        self.assertEqual(res.pieces[-1].end_s, 10.0)
        self.assertEqual(rf.leaked_frames(scene, plan, res), [])

    def test_appear_and_gone_to_the_exact_frame(self):
        spans = {1: [(3.0, 4.0, 1), (10.033, 13.0, 2), (20.5, 22.4, 1), (26.0, 27.0, 3)]}
        sc = rf.build_sidecar(STEM, [35.0], [], tracks=(),
                              screens={"spans": spans, "learn": [("end", 1, 3.5), ("start", 1, 11.0)], "phase": 0.07})
        plan, res, scene, _ = run(sc, spans)
        self.assertEqual([(k["start_s"], k["end_s"]) for k in sc["summary"]["kept"]][0][0], 0.0)
        bounds = self.screen_bounds(res)
        self.assertEqual([(b["side"], b["method"]) for b in bounds],
                         [("end", "detected"), ("start", "detected"), ("end", "detected")])
        # pieces: [0.0 .. first frame of the end screen at 3.0) is take 1 (implicit); take 2 from the frame
        # after the start screen (13.0) to the end screen's first frame (20.5 -> 20.5333 on the grid)
        self.assertEqual([(round(p.start_s, 4), round(p.end_s, 4)) for p in res.pieces],
                         [(1.0, 3.0), (13.0, round(frame_at_or_after(20.5), 4))])
        self.assertEqual(res.fallbacks, [])

    def test_a_reference_that_does_not_match_the_video_is_found_from_the_video_itself(self):
        """Another colour pipeline: the live reference matches no decoded frame. The
        screen is then found from the video's own frame where it is known to be up
        (the learn's capture; the first sample that saw it), loudly."""
        spans = {1: [(10.0, 14.0, 7), (20.0, 23.0, 7)]}
        sc = rf.build_sidecar(STEM, [40.0], [("take", 1, 16.0)], tracks=(),
                              screens={"spans": spans, "learn": [("end", 1, 12.0)]})
        for r in sc["auto_takes"]["references"]:
            r["thumb"] = rf.pattern(999).hex()             # what GDI saw, unlike anything decoded
        plan, res, scene, _ = run(sc, spans)
        bounds = self.screen_bounds(res)
        self.assertEqual([(b["method"], b.get("anchored"), b["bound_s"]) for b in bounds],
                         [("detected", "video", 10.0), ("detected", "video", 20.0)])
        self.assertEqual(len([f for f in res.fallbacks if "did not match the decoded video" in f]), 2)

    def test_the_video_anchor_allows_for_the_capture_lag(self):
        """The video shows a change ~40 ms after the sampler sees it. With the live
        reference matching nothing, the anchor must sit inside the screen, not on the
        still slide before it (review, second pass)."""
        sampled = {1: [(2.5, 4.0, 7), (6.5, 10.03, 3), (10.03, 14.0, 7)]}
        shown = {1: [(a + 0.04, b + 0.04, p) for a, b, p in sampled[1]]}
        sc = rf.build_sidecar(STEM, [30.0], [("take", 1, 6.0)], tracks=(),
                              screens={"spans": sampled, "learn": [("end", 1, 3.0)], "phase": 0.05})
        for r in sc["auto_takes"]["references"]:
            r["thumb"] = rf.pattern(999).hex()
        plan, res, scene, _ = run(sc, shown)
        end = [b for b in self.screen_bounds(res) if b["event"] != 1][-1]
        self.assertEqual((end["method"], end.get("anchored")), ("detected", "video"))
        self.assertAlmostEqual(end["bound_s"], frame_at_or_after(10.07), places=6)
        self.assertAlmostEqual(res.pieces[-1].end_s, frame_at_or_after(10.07), places=6)

    def test_a_decode_failure_falls_back_loudly_toward_cutting_more(self):
        spans = {1: [(10.0, 14.0, 7), (20.0, 23.0, 8)]}
        sc = rf.build_sidecar(STEM, [40.0], [], tracks=(),
                              screens={"spans": spans, "learn": [("end", 1, 12.0), ("start", 1, 21.0)]})
        plan, res, scene, _ = run(sc, spans, fail=("thumbs",))
        bounds = self.screen_bounds(res)
        self.assertEqual([b["method"] for b in bounds], ["stamp", "stamp"])
        end, start = bounds
        self.assertLessEqual(end["bound_s"], 12.0 - 0.25)                     # before the learn press
        gone = next(e for e in sc["events"] if e.get("change") == "gone" and e["screen"] == "start")
        self.assertGreater(start["bound_s"], gone["media_s"])                 # after the screen was gone
        self.assertEqual(len([f for f in res.fallbacks if "screen" in f]), 2)
        self.assertIn("could not decode", end["fallback"])
        self.assertIn("its frames before the press may remain", end["fallback"])   # learned on: said honestly

    def test_a_screen_learned_after_more_than_the_lookback_is_said(self):
        spans = {1: [(2.0, 30.0, 7)]}
        sc = rf.build_sidecar(STEM, [40.0], [("take", 1, 1.0)], tracks=(),
                              screens={"spans": spans, "learn": [("end", 1, 29.0)]})
        cfg = c.RenderConfig(screen_lookback_s=5.0)
        plan, res, scene, _ = run(sc, spans, cfg=cfg)
        [b] = self.screen_bounds(res)
        self.assertEqual(b["method"], "detected")
        self.assertIn("followed it no further", b["warning"])
        self.assertTrue(any("followed it no further" in f for f in res.fallbacks), res.fallbacks)   # loud
        self.assertAlmostEqual(b["bound_s"], 24.0, delta=1 / rf.FPS)
        plan, res, scene, _ = run(sc, spans)                                 # the default reaches back to 2.0
        self.assertEqual(self.screen_bounds(res)[0]["bound_s"], 2.0)
        self.assertEqual(res.pieces[-1].end_s, 2.0)                          # take 1: after its patch, up to 2.0
        self.assertEqual(res.fallbacks, [])

    def test_following_back_stops_where_the_take_starts(self):
        """The run is only followed back to the interval's own start: what lies
        before is cut anyway, so a long-up screen costs no decoding past it."""
        spans = {1: [(2.0, 30.0, 7)]}
        sc = rf.build_sidecar(STEM, [40.0], [("take", 1, 20.0)], tracks=(),
                              screens={"spans": spans, "learn": [("end", 1, 29.0)]})
        plan, res, scene, an = run(sc, spans)
        [b] = self.screen_bounds(res)
        self.assertEqual(b["method"], "detected")
        self.assertEqual([f for f in res.fallbacks if "screen" in f], [])
        self.assertGreaterEqual(min(c[2] for c in an.calls if c[0] == "thumbs"), 20.0)
        self.assertEqual(res.pieces, [])                                     # the take held only the screen

    def test_a_reference_the_sidecar_lacks_falls_back_loudly(self):
        spans = {1: [(10.0, 14.0, 7), (20.0, 23.0, 8)]}
        sc = rf.build_sidecar(STEM, [40.0], [], tracks=(),
                              screens={"spans": spans, "learn": [("end", 1, 12.0), ("start", 1, 21.0)]})
        sc["auto_takes"]["references"] = []
        plan, res, scene, _ = run(sc, spans)
        self.assertTrue(any("does not hold" in n for n in plan.notes), plan.notes)
        end, start = self.screen_bounds(res)
        self.assertEqual((end["method"], start["method"]), ("stamp", "stamp"))
        self.assertLessEqual(end["bound_s"], 12.0 - 0.25 - render.FALLBACK_MARGIN_S + 1e-6)
        gone = next(e for e in sc["events"] if e.get("change") == "gone" and e["screen"] == "start")
        self.assertGreater(start["bound_s"], gone["media_s"])
        self.assertEqual(len([f for f in res.fallbacks if "does not hold" in f]), 2)

    def test_the_digest_covers_the_references(self):
        spans = {1: [(10.0, 14.0, 7)]}
        sc = rf.build_sidecar(STEM, [40.0], [], tracks=(), screens={"spans": spans, "learn": [("end", 1, 12.0)]})
        before = render.source_digest(sc)
        sc["auto_takes"]["references"][0]["thumb"] = "00" * 160
        self.assertNotEqual(render.source_digest(sc), before)

    def test_screen_edge_needs_matching_sizes(self):
        ref = {"thumb": b"\0" * 160, "screen": "end", "period_s": 0.25}
        lo, hi, anchor, why = render.screen_edge([(0.0, b"\0" * 10)], ref, 0.0, "end", rf.scr.Thresholds(),
                                                 rf.scr.Thresholds())
        self.assertIsNone(lo)
        self.assertIn("pixels", why)


def fuzz_case(rnd):
    nseg = rnd.randint(1, 3)
    durs = [round(rnd.uniform(18, 36), 2) for _ in range(nseg)]
    spans, learns, presses, forgets = {}, [], [], []
    for k, d in enumerate(durs, 1):
        t, sp = 2.0, []
        while t < d - 4:
            straight = bool(sp) and rnd.random() < 0.25              # sometimes straight to the next screen
            t += 0.0 if straight else rnd.uniform(1.5, 6)
            length = rnd.uniform(0.8, 5)
            if t + length > d - 1.5:
                break
            pid = rnd.choice([p for p in (1, 1, 2, 2, 3) if not (straight and p == sp[-1][2])])
            sp.append((round(t, 3), round(t + length, 3), pid))
            t += length
        spans[k] = sp
    every = [(k, a, b, p) for k in spans for a, b, p in spans[k]]
    for kind, pid in (("end", 1), ("start", 2)):
        cands = [(k, a, b) for k, a, b, p in every if p == pid]
        if cands and rnd.random() < 0.85:
            k, a, b = rnd.choice(cands)
            learns.append((kind, k, round(rnd.uniform(a + 0.3, max(a + 0.31, b - 0.1)), 3)))
    if every and rnd.random() < 0.15:                       # learn one screen as the other kind too
        k, a, b, p = rnd.choice(every)
        learns.append(("start" if p == 1 else "end", k, round(rnd.uniform(a + 0.3, max(a + 0.31, b - 0.1)), 3)))
    for _ in range(rnd.randint(0, 6)):                      # presses, away from the screens
        k = rnd.randint(1, nseg)
        t = round(rnd.uniform(1.5, durs[k - 1] - 1.5), 3)
        if all(not (a - 1.5 <= t <= b + 1.5) for a, b, _ in spans[k]):
            presses.append((rnd.choice(["take", "take", "correct", "mark"]), k, t))
    if learns and rnd.random() < 0.1:
        kind, k, t = rnd.choice(learns)
        forgets.append((kind, k, round(min(durs[k - 1] - 1, t + rnd.uniform(1, 10)), 3)))
    return durs, spans, learns, presses, forgets


class FuzzTest(unittest.TestCase):
    def test_no_screen_or_fiducial_frame_is_ever_cut_in(self):
        rnd = random.Random(1008)
        edges = 0
        for case in range(150):
            durs, spans, learns, presses, forgets = fuzz_case(rnd)
            tracks = rnd.choice([(), ("system",)])
            spec = {"spans": spans, "learn": learns, "forget": forgets, "hz": 4.0,
                    "phase": round(rnd.uniform(0, 0.25), 3)}
            sc = rf.build_sidecar(STEM, durs, presses, tracks=tracks, screens=spec, source="hotkey")
            plan, res, scene, _ = run(sc, spans)
            with self.subTest(case=case, spans=spans, learns=learns, presses=presses, forgets=forgets):
                self.assertEqual(rf.leaked_frames(scene, plan, res), [])
                self.assertEqual([f for f in res.fallbacks if "screen" in f], [])
                for p in res.pieces:
                    iv = plan.intervals[p.interval]
                    sp = spans[p.segment]
                    learned = rf.ScreenSim.learned_pattern
                    if iv.start.fiducial.style == "screen":      # a take a screen opened never starts on a learned one
                        t0 = p.phase + p.k1 / p.fps
                        on = rf.screen_at(sp, t0)
                        self.assertNotIn(on, {learned(sc["_sim"], k, p.segment, t0) for k in ("start", "end")} - {None},
                                         (t0, sp))
                    if iv.end.fiducial.style == "screen":        # and one a screen closed never ends on the end screen
                        t1 = p.phase + (p.k2 - 1) / p.fps
                        on = rf.screen_at(sp, t1)
                        self.assertTrue(on is None or on != learned(sc["_sim"], "end", p.segment, t1 + 1.0), (t1, sp))
                    for bd in (iv.start, iv.end):
                        if bd.fiducial.style != "screen":
                            continue
                        edges += 1
                        st = bd.fiducial.stamp_s
                        kind = "end" if bd.side == "end" else "start"
                        pat = learned(sc["_sim"], kind, p.segment, st + 0.01)
                        if bd.side == "end":     # the run of the learned end screen the event saw
                            run_ = next((a, b) for a, b, pid in spans[p.segment]
                                        if a - 0.6 <= st <= b and pid == pat)
                        else:                    # the learned start screen that had just gone
                            run_ = max(((a, b) for a, b, pid in spans[p.segment] if b <= st + 1e-6 and pid == pat),
                                       key=lambda r: r[1])
                        kept_in = [k for k in range(p.k1, p.k2) if run_[0] <= p.phase + k / p.fps < run_[1]]
                        self.assertEqual(kept_in, [], (bd.side, run_))
                        if not tracks:           # no seam moves: the cut sits on the screen's own edge
                            edge = frame_at_or_after(run_[0]) if bd.side == "end" else frame_at_or_after(run_[1])
                            self.assertAlmostEqual(p.end_s if bd.side == "end" else p.start_s, edge, places=6)
        self.assertGreater(edges, 100)


class PatternAgreementTest(unittest.TestCase):
    def test_fake_ffmpeg_draws_what_the_fixtures_draw(self):
        import importlib.util
        from tests import REPO
        spec = importlib.util.spec_from_file_location("fake_ffmpeg_patterns", REPO / "tests" / "fake_ffmpeg.py")
        src = (REPO / "tests" / "fake_ffmpeg.py").read_text(encoding="utf-8")
        ns = {}
        start, end = src.index("def screen_pixel"), src.index("def sample_at")
        ns["hex_rgb"] = lambda c: bytes((int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)))
        exec(src[start:end], ns)                       # the two pure functions only: no process, no argv
        self.assertIsNotNone(spec)
        f = {"video": [{"t0": 0.5, "t1": 0.7, "color": "#FF00FF", "where": "full"}],
             "screens": [{"t0": 1.0, "t1": 2.0, "pattern": 7}], "screen_noise": 3, "content_seed": 100000}
        for t in (0.0, 0.6, 1.2, 1.9667, 2.5):
            rgb = ns["thumb_rgb"](f, t, 30, 16, 10)
            self.assertEqual(rgb[0::3], rf.thumb_at(f, t, 30, (16, 10)), t)


class EndToEndTest(RunHarness, unittest.TestCase):
    def test_renders_through_fake_ffmpeg_and_records_the_screen_edges(self):
        spans = {1: [(4.0, 6.0, 1), (12.0, 13.5, 2)], 2: [(5.0, 8.0, 1)]}
        sc = rf.build_sidecar(STEM, [20.0, 15.0], [], screens={"spans": spans,
                                                               "learn": [("end", 1, 5.0), ("start", 1, 12.5)]})
        self.record(sc, screen_spans=spans)
        dry = render.Renderer(self.cfg).render("last", dry_run=True)
        self.assertTrue(dry.ok, dry.message)
        self.assertTrue(any("(end screen, learned on)" in line for line in dry.report), dry.report)
        res = render.Renderer(self.cfg).render("last")
        self.assertTrue(res.ok, res.message)
        block = self.sidecar(STEM)["render"]
        screen = [b for b in block["boundaries"] if b.get("style") == "screen"]
        self.assertEqual([(b["side"], b["method"], b["bound_s"]) for b in screen],
                         [("end", "detected", 4.0), ("start", "detected", 13.5), ("end", "detected", 5.0)])
        thumbs = [a for a in map(json.loads, self.argv_log.read_text().splitlines())
                  if "rawvideo" in a and any("scale=20:12" in x for x in a) and not any("crop=" in x for x in a)]
        self.assertTrue(thumbs)
        self.assertTrue(all("-nostdin" in a for a in thumbs))
        self.assertEqual(os.listdir(self.folder).count(f"{STEM}.cut.mp4"), 1)


if __name__ == "__main__":
    unittest.main()
