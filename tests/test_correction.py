"""Session C1c: the correction chart (events.py), one test per row, the
consequences the architect accepted, and the pill's promise: the hint it
shows for a key is what that key's next press does.

The chart (ratified 2026-10-07): each take keeps a two-level undo stack of
its original presses, open then close. A correction pops the most recent one
and re-issues the same kind of boundary at the press: popping a close moves
the close here; popping an open drops the take and opens a fresh one here.

Pure: no files, no processes, no clock."""

import copy
import json
import random
import unittest

from peep import events as ev, pill
from peep.events import Press
from tests.test_events import Session, cli, hk


def state_of(s: Session, at: float | None = None) -> dict:
    """The take state as the pill gets it: published to active.json (a JSON
    round trip), not the model's own objects."""
    return json.loads(json.dumps(s.m.take_state(at)))


class ChartRowTest(unittest.TestCase):
    """Every row of the chart, in its order."""

    # -- No take yet ------------------------------------------------------------------

    def test_no_take_yet__take__take_1_opens_here(self):
        s = Session()
        r = s.press(hk("take", 105))
        self.assertEqual((r["action"], r["take"], r["media_s"]), ("open", 1, 5.0))
        self.assertEqual(s.m.takes[0]["undo"], ["open"])

    def test_no_take_yet__correction__ignored_nothing_to_correct(self):
        s = Session()
        r = s.press(hk("correct", 105))
        self.assertEqual((r["accepted"], r["ignored"], r["action"]), (False, "nothing-to-correct", None))
        self.assertEqual(r["correction"], {"action": "ignored", "reason": "nothing-to-correct"})
        self.assertEqual(s.m.takes, [])                                  # no stack, no take
        self.assertEqual(pill.feedback_text(ev.press_feedback(r, 1), {"correct": "⌫"}), "⌫ nothing to correct")

    # -- Take open --------------------------------------------------------------------

    def test_take_open__take__take_closes_here(self):
        s = Session()
        s.press(hk("take", 105))
        r = s.press(hk("take", 112))
        self.assertEqual((r["action"], r["take"]), ("close", 1))
        self.assertEqual(s.m.takes[0]["undo"], ["open", "close"])
        self.assertEqual(s.end(30.0)["kept"], [{"segment": 1, "start_s": 5.0, "end_s": 12.0, "take": 1}])

    def test_take_open__correction__dropped_from_its_open_and_a_fresh_take_opens_here(self):
        s = Session()
        s.press(hk("take", 105))
        r = s.press(hk("correct", 112))
        self.assertEqual((r["action"], r["discarded_take"], r["take"]), ("dropped-take", 1, 2))
        self.assertEqual(r["correction"]["boundary"]["side"], "open")
        self.assertEqual(r["correction"]["boundary"]["media_s"], 12.0)
        t1, t2 = s.m.takes
        self.assertEqual((t1["status"], t1["discarded_by"], t1["undo"]), ("discarded", r["id"], []))
        self.assertEqual((t2["open"]["media_s"], t2["undo"]), (12.0, ["open"]))
        s.press(hk("take", 120))
        self.assertEqual(s.end(30.0)["kept"], [{"segment": 1, "start_s": 12.0, "end_s": 20.0, "take": 2}])

    def test_take_open__correction_again__drops_that_fresh_take_too_flubbed_again(self):
        s = Session()
        s.press(hk("take", 105))
        s.press(hk("correct", 112))
        r = s.press(hk("correct", 115))
        self.assertEqual((r["action"], r["discarded_take"], r["take"]), ("dropped-take", 2, 3))
        self.assertEqual(s.m.takes[-1]["undo"], ["open"])
        summary = s.end(30.0)
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 15.0, "end_s": 30.0, "take": 3}])
        self.assertEqual((summary["takes"], summary["takes_discarded"]), (1, 2))

    # -- Take closed ------------------------------------------------------------------

    def closed(self) -> Session:
        s = Session()
        s.press(hk("take", 105))
        s.press(hk("take", 110))           # closed early
        return s

    def test_take_closed__take__next_take_opens_and_the_previous_is_final(self):
        s = self.closed()
        r = s.press(hk("take", 120))
        self.assertEqual((r["action"], r["take"]), ("open", 2))
        self.assertEqual((s.m.takes[0]["undo"], s.m.takes[1]["undo"]), ([], ["open"]))
        r = s.press(hk("correct", 125))                 # corrections now reach take 2 only
        self.assertEqual((r["action"], r["discarded_take"]), ("dropped-take", 2))
        self.assertEqual(s.m.takes[0]["status"], "closed")
        self.assertEqual(s.m.takes[0]["close"]["media_s"], 10.0)

    def test_take_closed__correction_1__the_close_moves_here_and_the_gap_is_back_in(self):
        s = self.closed()
        r = s.press(hk("correct", 114.2))
        self.assertEqual((r["action"], r["take"]), ("moved-close", 1))
        c = r["correction"]
        self.assertEqual((c["from"]["media_s"], c["boundary"]["media_s"], c["boundary"]["side"]), (10.0, 14.2, "close"))
        self.assertEqual((c["moved_s"], c["undo"]), (4.2, ["open"]))
        t = s.m.takes[0]
        self.assertEqual((t["close"]["media_s"], t["undo"], t["superseded_closes"][0]["event"]), (14.2, ["open"], 2))
        self.assertEqual(s.end(30.0)["kept"], [{"segment": 1, "start_s": 5.0, "end_s": 14.2, "take": 1}])
        self.assertEqual(pill.feedback_text(ev.press_feedback(r, 1), {"correct": "⌫"}), "⌫ close moved +4.2 s")

    def test_take_closed__correction_2__pops_the_open_the_whole_take_drops_and_a_fresh_one_opens(self):
        s = self.closed()
        s.press(hk("correct", 114))
        r = s.press(hk("correct", 118))
        self.assertEqual((r["action"], r["discarded_take"], r["take"]), ("dropped-take", 1, 2))
        self.assertEqual(s.m.takes[-1]["undo"], ["open"])
        self.assertEqual(pill.feedback_text(ev.press_feedback(r, 2), {"correct": "⌫"}),
                         "⌫ take 1 dropped · take 2 started")
        s.press(hk("take", 125))
        self.assertEqual(s.end(30.0)["kept"], [{"segment": 1, "start_s": 18.0, "end_s": 25.0, "take": 2}])

    def test_take_closed__correction_3__drops_that_fresh_take_and_opens_another(self):
        s = self.closed()
        s.press(hk("correct", 114))
        s.press(hk("correct", 118))
        r = s.press(hk("correct", 121))
        self.assertEqual((r["action"], r["discarded_take"], r["take"]), ("dropped-take", 2, 3))
        self.assertEqual([t["status"] for t in s.m.takes], ["discarded", "discarded", "open"])

    # -- Any state ----------------------------------------------------------------------

    def test_any_state__pause_resume__never_touch_the_stack(self):
        for setup in ([], [("take", 105)], [("take", 105), ("take", 108)], [("take", 105), ("take", 108),
                                                                           ("correct", 109.5)]):
            with self.subTest(setup=setup):
                s = Session()
                for kind, q in setup:
                    s.press(cli(kind, q))
                before = copy.deepcopy(s.m._stack_state())
                s.pause_at(110, end_qpc=110.5, duration=10.5)
                self.assertEqual(s.m._stack_state(), before)
                s.resume_at(200, epoch=300.0)
                self.assertEqual(s.m._stack_state(), before)

    def test_any_state__a_close_moved_later_across_a_pause_spans_the_pause(self):
        s = Session()
        s.press(hk("take", 103))
        s.press(hk("take", 106))                             # closed early, in segment 1
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        s.resume_at(200, epoch=300.0)
        r = s.press(hk("correct", 304))                      # ① in segment 2
        self.assertEqual(r["correction"]["moved_s"], 4.5 + 4.0)   # 10.5 - 6 of segment 1, then 4 of segment 2
        summary = s.end(20.0)
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 3.0, "end_s": 10.5, "take": 1},
                                           {"segment": 2, "start_s": 0.0, "end_s": 4.0, "take": 1}])

    def test_any_state__a_correction_while_paused_takes_effect_at_the_boundary(self):
        s = Session()
        s.press(hk("take", 103))
        s.press(hk("take", 106))
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        moved = s.press(cli("correct", 150))                 # ①: the close moves to the pause
        dropped = s.press(cli("correct", 160))               # ②: a fresh take opens at the resume
        s.resume_at(200, epoch=300.0)
        self.assertEqual((moved["action"], moved["after_segment"]), ("moved-close", 1))
        self.assertEqual(dropped["action"], "dropped-take")
        self.assertEqual(s.end(5.0)["kept"], [{"segment": 2, "start_s": 0.0, "end_s": 5.0, "take": 2}])

    def test_any_state__mark__is_never_a_boundary(self):
        for setup in ([], [("take", 105)], [("take", 105), ("take", 108)]):
            with self.subTest(setup=setup):
                s = Session()
                for kind, q in setup:
                    s.press(cli(kind, q))
                before = copy.deepcopy(s.m.takes)
                r = s.press(cli("mark", 109))
                self.assertEqual(r["action"], "mark")
                self.assertEqual(s.m.takes, before)

    def test_any_state__correction_within_1_s_of_the_previous_accepted_press__debounced(self):
        cases = {"after a correction": [hk("take", 105), hk("correct", 108)],
                 "after a take close": [hk("take", 105), hk("take", 108)],
                 "after a take open": [hk("take", 108)],
                 "after a mark": [hk("take", 105), hk("mark", 108)],
                 "after a WSL take": [cli("take", 105), cli("take", 108)],
                 "after a WSL mark": [hk("take", 105), cli("mark", 108)]}
        for name, setup in cases.items():
            with self.subTest(name):
                s = Session()
                for p in setup:
                    self.assertTrue(s.press(p)["accepted"])
                before = copy.deepcopy(s.m.takes)
                r = s.press(hk("correct", 108.9))
                self.assertEqual((r["accepted"], r["ignored"]), (False, "debounce"))
                self.assertEqual(r["correction"], {"action": "ignored", "reason": "debounce"})
                self.assertEqual(s.m.takes, before)                    # unchanged
                self.assertEqual(pill.feedback_text(ev.press_feedback(r, 9), {"correct": "⌫"}),
                                 "⌫ ignored — too fast")
                self.assertTrue(s.press(hk("correct", 109.1))["accepted"])   # past the window: honoured

    def test_every_accepted_press_opens_the_correction_window_but_an_ignored_one_does_not(self):
        """'The previous accepted press', literally: a pause or a resume too."""
        s = Session()
        s.press(hk("take", 101))
        s.pause_at(105, end_qpc=105.2, duration=5.2)
        r = s.press(hk("correct", 105.6))
        self.assertEqual((r["ignored"], r["debounce"]["after"]), ("debounce", "pause-toggle"))
        s.resume_at(120, epoch=121.0)
        self.assertEqual(s.press(hk("correct", 120.5))["ignored"], "debounce")      # after the resume
        s.press(hk("take", 125))                                   # closes take 1
        bounce = s.press(hk("take", 125.4))                        # ignored: opens no window
        self.assertEqual(bounce["ignored"], "debounce")
        self.assertTrue(s.press(hk("correct", 126.1))["accepted"])    # 1.1 s after the accepted close

    def test_wsl_corrections_are_never_debounced(self):
        s = Session()
        s.press(cli("take", 105))
        s.press(cli("take", 106))
        self.assertEqual([s.press(cli("correct", q))["action"] for q in (106.1, 106.2, 106.3)],
                         ["moved-close", "dropped-take", "dropped-take"])


class ConsequencesTest(unittest.TestCase):
    """What the planner pointed out and the architect accepted."""

    def test_only_an_explicit_take_press_starts_a_take(self):
        s = Session()
        for q in (102, 104, 106):
            s.press(hk("correct", q))
        summary = s.end(20.0)
        self.assertTrue(summary["whole"])
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 0.0, "end_s": 20.0, "take": None}])

    def test_dropping_every_take_keeps_nothing_and_renders_no_cut(self):
        from peep import render
        s = Session()
        s.press(hk("take", 103))
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        s.press(cli("correct", 150))       # drops take 1; the fresh take would open at a resume that never comes
        summary = s.end(10.5)
        self.assertFalse(summary["whole"])                      # in take mode: not the whole recording
        self.assertEqual(summary["kept"], [])
        self.assertFalse(render.should_render("always", summary))     # C1b's "every take discarded" path

    def test_closing_a_fresh_take_at_once_leaves_an_empty_or_near_empty_take(self):
        s = Session()
        s.press(cli("take", 103))
        s.press(cli("take", 108))
        s.press(cli("correct", 109))
        s.press(cli("correct", 110))
        s.press(cli("take", 110.1))                             # ①②, then Take
        summary = s.end(20.0)
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 10.0, "end_s": 10.1, "take": 2}])
        self.assertLess(summary["kept_s"], 0.25)                # under render.min_interval_ms: the render drops it

    def test_previous_take_is_final_once_the_next_opens(self):
        s = Session()
        s.press(cli("take", 101))
        s.press(cli("take", 103))
        s.press(cli("take", 105))                               # take 2 opens: take 1 is final
        s.press(cli("correct", 106))                            # drops take 2
        s.press(cli("correct", 107))                            # drops take 3, never take 1
        self.assertEqual([t["status"] for t in s.m.takes], ["closed", "discarded", "discarded", "open"])

    def test_the_sidecar_says_which_rule_decided_the_takes(self):
        s = Session()
        self.assertEqual(s.m.take_state()["rule"], ev.TAKE_RULE)
        self.assertEqual(ev.TAKE_RULE, "correction/1")


class NextEffectTest(unittest.TestCase):
    """The chart as one pure function."""

    def test_the_chart_table(self):
        rows = [({"mode": "none", "undo": [], "next_take": 1}, "take", {"action": "open", "take": 1, "previous": None}),
                ({"mode": "none", "undo": [], "next_take": 1}, "correct",
                 {"action": "ignored", "reason": "nothing-to-correct"}),
                ({"mode": "open", "take": 2, "undo": ["open"], "next_take": 3}, "take", {"action": "close", "take": 2}),
                ({"mode": "open", "take": 2, "undo": ["open"], "next_take": 3}, "correct",
                 {"action": "dropped-take", "dropped": 2, "take": 3}),
                ({"mode": "closed", "take": 2, "undo": ["open", "close"], "next_take": 3}, "take",
                 {"action": "open", "take": 3, "previous": 2}),
                ({"mode": "closed", "take": 2, "undo": ["open", "close"], "next_take": 3}, "correct",
                 {"action": "moved-close", "take": 2}),
                ({"mode": "closed", "take": 2, "undo": ["open"], "next_take": 3}, "retake",
                 {"action": "dropped-take", "dropped": 2, "take": 3})]
        for state, kind, want in rows:
            with self.subTest(state=state, kind=kind):
                self.assertEqual(ev.next_effect(state, kind), want)

    def test_only_stack_keys(self):
        with self.assertRaises(ValueError):
            ev.next_effect({"mode": "none"}, "mark")


class HintEqualsEffectTest(unittest.TestCase):
    """The pill's hint is computed from the published take state by the same
    function the model decides with; here it is held against what the next
    press actually does, for every chart row and for random sequences."""

    WORDS = {"open": {"start take", "next take"}, "close": {"close"}, "moved-close": {"move close here"},
             "dropped-take": {"restart take", "drop take {dropped}, restart"}}

    def assert_hint_matches_press(self, s: Session, kind: str, q: float, source: str = "hotkey"):
        state = state_of(s, q)
        predicted = ev.next_effect(state, kind)
        words = pill.hint_words(state)
        model = copy.deepcopy(s.m)
        rec = model.press(Press(kind, q, source=source), q)
        if rec["ignored"] == "debounce":
            return None                                        # time, not state: said by the feedback line
        if predicted["action"] == "ignored":
            self.assertEqual(rec["ignored"], predicted["reason"])
            self.assertNotIn("correct", words)                 # the hint never offers a key that does nothing
            return rec
        self.assertTrue(rec["accepted"], rec)
        self.assertEqual(rec["action"], predicted["action"])
        self.assertEqual(rec["take"], predicted["take"])
        if predicted["action"] == "dropped-take":
            self.assertEqual(rec["discarded_take"], predicted["dropped"])
        said = words["take" if kind == "take" else "correct"]
        allowed = {w.format(dropped=predicted.get("dropped")) for w in self.WORDS[rec["action"]]}
        self.assertIn(said, allowed)
        return rec

    def test_every_chart_row(self):
        rows = {"no take yet": [],
                "take open": [("take", 105)],
                "take open after one correction": [("take", 105), ("correct", 108)],
                "take closed": [("take", 105), ("take", 110)],
                "take closed after ①": [("take", 105), ("take", 110), ("correct", 114)],
                "take closed then ①②": [("take", 105), ("take", 110), ("correct", 114), ("correct", 117)],
                "second take open": [("take", 105), ("take", 110), ("take", 113)]}
        for name, setup in rows.items():
            for kind in ("take", "correct"):
                with self.subTest(row=name, key=kind):
                    s = Session()
                    for k, q in setup:
                        self.assertTrue(s.press(hk(k, q))["accepted"], (k, q))
                    self.assertIsNotNone(self.assert_hint_matches_press(s, kind, 125.0))

    def test_random_sequences_with_pauses_and_both_sources(self):
        rnd = random.Random(1007)
        checked = 0
        for case in range(300):
            s = Session(debounce=1.0)
            q, seg = 100.0, 1
            for _ in range(rnd.randint(1, 18)):
                q += rnd.uniform(0.1, 2.5)
                what = rnd.choice(["take", "take", "correct", "correct", "mark", "pause"])
                src = rnd.choice(["hotkey", "hotkey", "cli"])
                if what == "pause":
                    if not s.m.paused:
                        s.pause_at(q, end_qpc=q + 0.3, duration=q + 0.3 - s.m.segments[seg]["epoch_qpc"],
                                   source="cli")
                    else:
                        seg += 1
                        s.resume_at(q, epoch=q + 1.0, source="cli")
                        q += 1.0
                    continue
                if what == "mark":
                    s.press(cli("mark", q))
                    continue
                with self.subTest(case=case, at=q, key=what):
                    if self.assert_hint_matches_press(s, what, q, src) is not None:
                        checked += 1
                s.press(Press(what, q, source=src))
        self.assertGreater(checked, 1000)


class TakeStateTest(unittest.TestCase):
    def test_kept_and_take_time_as_of_an_instant(self):
        s = Session()
        st = state_of(s, 141.0)
        self.assertEqual((st["mode"], st["kept_s"], st["take_s"], st["at_s"]), ("none", 41.0, None, 41.0))
        self.assertTrue(st["growing"]["kept"])
        s.press(hk("take", 110))
        s.press(hk("take", 188))                       # take 1: 1:18
        s.press(hk("take", 244))                       # take 2 opens at 2:24
        st = state_of(s, 292.0)                        # 3:12
        self.assertEqual((st["mode"], st["take"], st["take_s"], st["kept_s"]), ("open", 2, 48.0, 126.0))
        self.assertEqual(st["growing"], {"kept": True, "take": True})
        s.press(hk("take", 300))                       # 3:20, closed: 2 takes, 2:14 kept
        st = state_of(s, 300.0)
        self.assertEqual((st["mode"], st["takes"], st["kept_s"], st["undo"]), ("closed", 2, 134.0, ["open", "close"]))
        self.assertEqual(st["growing"], {"kept": False, "take": False})

    def test_paused_state_never_grows(self):
        s = Session()
        s.press(hk("take", 105))
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        st = state_of(s, 150.0)
        self.assertEqual((st["paused"], st["take_s"], st["growing"]), (True, 5.5, {"kept": False, "take": False}))

    def test_press_feedback_carries_the_record_not_a_guess(self):
        s = Session()
        r = s.press(hk("take", 105))
        self.assertEqual(ev.press_feedback(r, 1), {"seq": 1, "event": 1, "qpc": 105.0, "kind": "take",
                                                  "action": "open", "ignored": None, "take": 1})
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        r = s.press(cli("correct", 150))
        fb = ev.press_feedback(r, 2)
        self.assertEqual((fb["action"], fb["dropped"], fb["take"], fb["at_boundary"]), ("dropped-take", 1, 2, 1))


if __name__ == "__main__":
    unittest.main()
