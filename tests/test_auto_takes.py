"""Session C1d: auto-takes in the event model and on the compact pill. Pure:
no files, no processes, no clock.

  RulesTableTest        every row of the ratified table, with its pill line
  ImplicitTakeTest      no takes = one take from the start; the first end screen closes it
  AlternationTest       a start only after an end (or no take open), an end only after a start
  CorrectionTest        automatic boundaries on the undo stack like pressed ones
  LearnTest             learning: on the appearance it is learned on, not retroactive,
                        re-learning, forgetting, refusals, paused
  NextEffectTest        the rules as the one pure function
  HintEqualsEffectTest  the pill's hint and feedback against what the model does, with
                        visual events in every state and in random sequences
  RecordTest            the sidecar block, active.json's auto_takes, unchanged without learning
"""

import copy
import json
import random
import unittest

from peep import config as c, events as ev, pill
from peep.events import Press
from tests.test_events import Session, hk

LABELS = pill.key_labels(c.AgentConfig())
THUMB = "40" * 160                       # a 16x10 thumbnail, as hex (the model stores it, never reads it)


def learn(kind, q, ref=None, **extra):
    x = {"screen": kind, "ref": ref or f"{kind}-{q}", "thumb": THUMB, "size": [16, 10], "stable": True,
         "waited_s": 0.1, **extra}
    return Press("learn", q, source="hotkey", extra=x)


def seen(kind, change, q, ref):
    return Press("screen", q, source="visual", extra={"screen": kind, "change": change, "ref": ref,
                                                      "score": {"mad": 1.0, "changed_pct": 0.5}, "samples": 2})


class Auto(Session):
    """A Session with helpers for screens: the agent's refs are learn's ref ids."""

    def learn(self, kind, q, **extra):
        r = self.press(learn(kind, q, **extra))
        self.ref = getattr(self, "ref", {})
        if r["accepted"]:
            self.ref[kind] = r["ref"]
        return r

    def appear(self, kind, q):
        return self.press(seen(kind, "appear", q, self.ref[kind]))

    def gone(self, kind, q):
        return self.press(seen(kind, "gone", q, self.ref[kind]))

    def fb(self, r, n=1):
        return pill.feedback_text(ev.press_feedback(r, n), LABELS)


def both_learned() -> Auto:
    """End learned with no take (it closes the implicit take 1), then start learned
    after it went away (pending), and gone: take 2 is open at 20."""
    s = Auto()
    s.learn("end", 110)
    s.gone("end", 112)
    s.learn("start", 115)
    s.gone("start", 120)
    return s


class RulesTableTest(unittest.TestCase):
    def test_end_screen_appears__no_take_open__ignored_and_the_pill_says_so(self):
        s = both_learned()
        s.press(hk("take", 130))                                     # take 2 closes by hand
        r = s.appear("end", 140)
        self.assertFalse(r["accepted"])
        self.assertEqual(r["ignored"], "no-take-open")
        self.assertEqual(s.m.takes[-1]["close"]["event"], s.m.events[-2]["id"])     # the press's close stands
        self.assertEqual(s.fb(r), "◇ end screen seen — no take open")

    def test_end_screen_appears__take_open__closes_at_the_first_frame_of_the_screen(self):
        s = both_learned()
        r = s.appear("end", 140.25)
        self.assertEqual((r["action"], r["take"]), ("close", 2))
        close = s.m.takes[1]["close"]
        self.assertEqual((close["event"], close["media_s"]), (r["id"], 40.25))     # its first sample
        self.assertEqual(s.m.takes[1]["undo"], ["open", "close"])
        self.assertEqual(s.fb(r), "◇ end screen → take 2 closed")

    def test_start_screen_appears__no_take_open__take_opens_when_it_goes_away(self):
        s = both_learned()
        s.appear("end", 140)                                          # closes take 2
        s.gone("end", 142)
        r = s.appear("start", 150)
        self.assertEqual((r["action"], r["take"]), ("pending", 3))
        self.assertEqual(len(s.m.takes), 2)                           # nothing opens yet
        self.assertEqual(s.fb(r), "◇ start screen up → take opens when it goes")
        g = s.gone("start", 154.5)
        self.assertEqual((g["action"], g["take"]), ("open", 3))
        self.assertEqual(s.m.takes[2]["open"]["media_s"], 54.5)       # the screen itself is not kept
        self.assertEqual(s.fb(g), "◇ start screen gone → take 3 opened")
        summary = s.end(60.0)
        self.assertEqual([(k["start_s"], k["end_s"]) for k in summary["kept"]],
                         [(0.0, 10.0), (20.0, 40.0), (54.5, 60.0)])

    def test_start_screen_appears__take_open__ignored_take_already_open(self):
        s = both_learned()                                            # take 2 open
        r = s.appear("start", 130)
        self.assertEqual(r["ignored"], "take-already-open")
        self.assertEqual(s.fb(r), "◇ start screen seen — take already open")
        g = s.gone("start", 133)                                      # its going away does nothing
        self.assertEqual((g["action"], g.get("take")), ("gone", None))
        self.assertIsNone(s.fb(g))
        self.assertEqual(len(s.m.takes), 2)

    def test_a_take_opened_by_hand_while_the_start_screen_is_up_wins(self):
        s = both_learned()
        s.appear("end", 140)
        s.gone("end", 141)
        s.appear("start", 150)                                        # pending
        s.press(hk("take", 152))                                      # T while it shows: take 3 opens here
        g = s.gone("start", 154)
        self.assertEqual(g["ignored"], "take-already-open")
        self.assertEqual(s.m.takes[2]["open"]["media_s"], 52.0)
        self.assertEqual(s.fb(g), "◇ start screen gone — take already open")


class ImplicitTakeTest(unittest.TestCase):
    def test_no_take_and_no_end_screen_is_unchanged(self):
        s = Auto()
        s.learn("start", 105)                    # learned, pending, but it never goes: no take
        summary = s.end(30.0)
        self.assertTrue(summary["whole"])
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 0.0, "end_s": 30.0, "take": None}])

    def test_the_first_end_screen_closes_the_implicit_take(self):
        s = Auto()
        s.learn("end", 105)                                          # learned while gone? no: learned on it
        s.gone("end", 106)
        r = s.appear("end", 112)                                      # already closed: no take open now
        self.assertEqual(r["ignored"], "no-take-open")
        t = s.m.takes[0]
        self.assertTrue(t["implicit"] and t["open"]["implicit"])
        self.assertEqual((t["open"]["media_s"], t["close"]["media_s"]), (0.0, 5.0))
        summary = s.end(30.0)
        self.assertFalse(summary["whole"])                            # take mode from then on
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 0.0, "end_s": 5.0, "take": 1}])

    def test_by_appearance_after_learning_elsewhere(self):
        s = Auto()
        s.learn("start", 104)                                         # start learned first: pending...
        s.gone("start", 106)                                          # ...take 1 opens at 6
        self.assertEqual(s.m.takes[0]["open"]["media_s"], 6.0)
        self.assertFalse(s.m.takes[0].get("implicit"))
        s2 = Auto()
        s2.learn("end", 104)                                          # learned with the implicit take open
        self.assertEqual(s2.m.takes[0]["close"]["media_s"], 4.0)

    def test_a_start_screen_before_any_take_cuts_what_came_before(self):
        """Read literally: with no take, nothing is open, so a start screen counts."""
        s = Auto()
        s.learn("end", 103)
        s.gone("end", 103.5)                                          # implicit take 1: 0-3
        s.learn("start", 104)
        s.gone("start", 108)
        s2 = Auto()
        s2.learn("start", 104)
        s2.gone("start", 108)
        self.assertEqual(s2.end(20.0)["kept"], [{"segment": 1, "start_s": 8.0, "end_s": 20.0, "take": 1}])

    def test_the_implicit_take_spans_pauses(self):
        s = Auto()
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        s.resume_at(130, epoch=131.0)
        s.learn("end", 135)                                           # segment 2, 4 s in
        summary = s.end(20.0)
        self.assertEqual([(k["segment"], k["start_s"], k["end_s"]) for k in summary["kept"]],
                         [(1, 0.0, 10.5), (2, 0.0, 4.0)])


class AlternationTest(unittest.TestCase):
    def test_alternation_falls_out_of_the_guards(self):
        s = Auto()
        s.learn("end", 110)                # implicit closed: end first, then...
        s.gone("end", 111)
        self.assertEqual(s.appear("end", 113)["ignored"], "no-take-open")     # end after end: ignored
        s.gone("end", 114)
        s.learn("start", 115)
        self.assertEqual(s.gone("start", 117)["action"], "open")             # start after end: opens
        self.assertEqual(s.appear("start", 120)["ignored"], "take-already-open")   # start after start
        s.gone("start", 121)
        self.assertEqual(s.appear("end", 125)["action"], "close")            # end after start: closes
        s.gone("end", 126)
        self.assertEqual(s.appear("start", 130)["action"], "pending")
        self.assertEqual(s.gone("start", 132)["action"], "open")
        summary = s.end(40.0)
        self.assertEqual([(k["start_s"], k["end_s"]) for k in summary["kept"]],
                         [(0.0, 10.0), (17.0, 25.0), (32.0, 40.0)])

    def test_a_start_screen_giving_way_straight_to_the_end_screen_opens_no_take(self):
        """No content came between them, so no take opens on top of the end screen,
        whichever of the two events the model hears first (the review's finding)."""
        for order in ("gone first", "appear first", "learned"):
            with self.subTest(order=order):
                s = both_learned()
                s.appear("end", 140)                                  # take 2 closes
                s.gone("end", 141)
                s.appear("start", 150)                                # pending
                if order == "gone first":                             # the sampler's order
                    g = s.gone("start", 155)
                    a = s.appear("end", 155)
                    self.assertEqual((g["action"], a["action"]), ("open", "close"))
                elif order == "appear first":
                    a = s.appear("end", 155)
                    g = s.gone("start", 155.25)
                    self.assertEqual((a["ignored"], g["ignored"]), ("no-take-open", "end-screen-up"))
                    self.assertEqual(s.fb(g), "◇ start screen gone — the end screen is up")
                else:                                                 # switched, and learned the end again
                    s.learn("end", 155)
                    g = s.gone("start", 155.5)
                    self.assertEqual(g["ignored"], "end-screen-up")
                kept = s.end(60.0)["kept"]
                self.assertEqual([(k["start_s"], k["end_s"]) for k in kept], [(0.0, 10.0), (20.0, 40.0)])

    def test_an_end_screen_learned_while_the_start_screens_going_arrives_first(self):
        """Switched from the start screen straight to the end page and pressed ] at once:
        the learn's capture settles while the sampler reports the start screen gone. The
        take that opened is closed where it opened, empty: an automatic open never wins
        over the end screen (review, second pass)."""
        s = both_learned()
        s.appear("end", 140)
        s.gone("end", 141)
        s.appear("start", 150)
        g = s.gone("start", 155.25)                           # opens take 3 at 55.25 ...
        r = s.learn("end", 155.0)                             # ... the end screen, up since 55.0
        self.assertEqual((g["action"], r["screen_effect"]), ("open", {"action": "close", "take": 3, "empty": True}))
        t3 = s.m.takes[2]
        self.assertEqual((t3["open"]["media_s"], t3["close"]["media_s"], t3["close"]["event"]), (55.25, 55.25, r["id"]))
        kept = s.end(60.0)["kept"]
        self.assertEqual([(k["start_s"], k["end_s"]) for k in kept], [(0.0, 10.0), (20.0, 40.0)])

    def test_an_automatic_boundary_older_than_the_last_press_is_ignored(self):
        """The sampler stamps its first matching sample but delivers a beat later:
        a press made meanwhile was decided first, and stays."""
        s = both_learned()                 # take 2 open since 20
        s.press(Press("take", 140.3, source="cli"))      # closed by hand at 40.3 ...
        s.press(Press("take", 140.5, source="cli"))      # ... and take 3 opened at 40.5 (WSL: no debounce)
        r = s.appear("end", 140.25)        # an end screen first seen at 40.25 arrives now
        self.assertEqual(r["ignored"], "earlier-than-the-last-boundary")
        self.assertEqual(s.fb(r), "◇ end screen seen — a press came first")
        self.assertIsNone(s.m.takes[-1]["close"])


class CorrectionTest(unittest.TestCase):
    """Correction (⌫) treats automatic boundaries like manual ones (C1c's chart)."""

    def test_an_automatic_close_can_be_moved(self):
        s = both_learned()
        s.appear("end", 140)
        r = s.press(hk("correct", 146.5))
        self.assertEqual((r["action"], r["correction"]["moved_s"]), ("moved-close", 6.5))
        self.assertEqual(s.m.takes[1]["close"]["media_s"], 46.5)
        self.assertEqual(s.m.takes[1]["superseded_closes"][0]["event"], s.m.events[-2]["id"])

    def test_an_automatically_opened_take_can_be_dropped_and_a_fresh_one_opens_at_the_press(self):
        s = both_learned()                                            # take 2 opened by the start screen
        r = s.press(hk("correct", 125))
        self.assertEqual((r["action"], r["discarded_take"], r["take"]), ("dropped-take", 2, 3))
        self.assertEqual(s.m.takes[2]["open"]["media_s"], 25.0)

    def test_the_implicit_take_is_corrected_like_any_take(self):
        s = Auto()
        s.learn("end", 110)
        s.gone("end", 112)
        one = s.press(hk("correct", 114))                             # ① the close moves to 14
        self.assertEqual((one["action"], s.m.takes[0]["close"]["media_s"]), ("moved-close", 14.0))
        two = s.press(hk("correct", 118))                             # ② the implicit take drops, 3 opens at 18
        self.assertEqual((two["action"], two["discarded_take"], two["take"]), ("dropped-take", 1, 2))
        self.assertEqual(s.end(30.0)["kept"], [{"segment": 1, "start_s": 18.0, "end_s": 30.0, "take": 2}])

    def test_a_correction_right_after_an_automatic_close_is_not_too_fast(self):
        """Learn presses and visual events are not take-key presses: they never open the
        correction's debounce window (the chart's last row is about double taps)."""
        s = both_learned()
        s.appear("end", 140)
        r = s.press(hk("correct", 140.4))
        self.assertTrue(r["accepted"], r)
        s2 = Auto()
        s2.learn("end", 110)
        self.assertTrue(s2.press(hk("correct", 110.3))["accepted"])

    def test_the_next_automatic_take_makes_the_previous_final(self):
        s = both_learned()
        s.appear("end", 140)
        s.gone("end", 141)
        s.appear("start", 150)
        s.gone("start", 152)
        self.assertEqual(s.m.takes[1]["undo"], [])
        self.assertEqual(s.m.takes[2]["undo"], ["open"])


class LearnTest(unittest.TestCase):
    def test_learning_an_end_screen_while_a_take_is_open_closes_it_at_the_press(self):
        s = Auto()
        s.press(hk("take", 105))
        r = s.learn("end", 130)
        self.assertEqual((r["action"], r["screen_effect"], r["take"]), ("learned", {"action": "close", "take": 1}, 1))
        self.assertEqual(s.m.takes[0]["close"]["event"], r["id"])     # the render refines it to the first frame
        self.assertEqual(s.fb(r), "⇥ end screen learned · take 1 closed")

    def test_learning_a_start_screen_counts_that_appearance(self):
        s = Auto()
        s.press(hk("take", 105))
        s.press(hk("take", 110))
        r = s.learn("start", 120)
        self.assertEqual(r["screen_effect"]["action"], "pending")
        self.assertEqual(s.fb(r), "⇥ start screen learned · opens a take when gone")
        self.assertEqual(s.gone("start", 123)["take"], 2)

    def test_learning_is_not_retroactive(self):
        """Appearances before the press do not count: the sampler only matches a
        screen once it is learned, and an event for a reference the model does not
        hold (yet, or any more) is recorded and ignored."""
        s = Auto()
        s.press(hk("take", 105))
        r = s.press(seen("end", "appear", 107, "end-1"))
        self.assertEqual(r["ignored"], "stale-reference")
        self.assertIsNone(s.m.takes[0]["close"])
        s.learn("end", 140, ref="end-1")
        self.assertEqual(s.m.takes[0]["close"]["media_s"], 40.0)     # this appearance, not the earlier one

    def test_relearning_replaces_the_reference_from_then_on(self):
        s = both_learned()
        old = s.ref["end"]
        r = s.learn("end", 130)                                       # a new end screen while take 2 is open
        self.assertEqual((r["action"], r["screen_effect"]["action"]), ("relearned", "close"))
        self.assertEqual(s.fb(r), "⇥ end screen re-learned · take 2 closed")
        self.assertEqual(s.press(seen("end", "gone", 131, old))["ignored"], "stale-reference")
        self.assertEqual([x["ref"] for x in s.m.reference_log if x["screen"] == "end"], [old, s.ref["end"]])
        self.assertEqual(s.m.reference_log[-1]["replaces"], old)

    def test_the_same_appearance_learned_again_counts_once(self):
        s = both_learned()
        s.appear("end", 140)                                          # closes take 2
        r = s.learn("end", 141, same_as_current=True)
        self.assertEqual(r["screen_effect"], {"action": "none", "reason": "same-appearance"})
        self.assertEqual(s.fb(r), "⇥ end screen re-learned")
        self.assertEqual(s.m.screen_stats["end"]["seen"], 2)          # learned on + 140, not 141

    def test_refusals_are_recorded_and_said(self):
        s = Auto()
        s.learn("end", 105)
        r = s.learn("start", 106, refused="same-as-end-screen")       # the agent compared the thumbnails
        self.assertEqual((r["accepted"], r["ignored"]), (False, "same-as-end-screen"))
        self.assertEqual(s.fb(r), "⇥ not learned: that is the end screen")
        self.assertIsNone(s.m.references["start"])
        self.assertEqual(s.m.screen_stats["start"]["last_learn"]["outcome"], "refused")
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        p = s.learn("start", 112)
        self.assertEqual(p["ignored"], "paused")
        self.assertEqual(s.fb(p), "⇥ start screen: not while paused")
        bad = s.press(Press("learn", 113, source="cli", extra={"screen": "sideways"}))
        self.assertEqual(bad["ignored"], "unknown-screen")
        none = s.press(Press("learn", 114, source="cli", extra={"screen": "end", "ref": "x"}))
        self.assertEqual(none["ignored"], "paused")

    def test_forget(self):
        s = both_learned()
        s.appear("end", 140)
        s.gone("end", 141)
        s.appear("start", 150)                                        # pending
        r = s.press(Press("forget", 151, source="cli", extra={"screen": "start"}))
        self.assertEqual((r["action"], r["dropped_pending"]), ("forgotten", s.m.events[-2]["id"]))
        self.assertEqual(s.fb(r), "⇥ start screen forgotten")
        self.assertEqual(s.gone("start", 153)["ignored"], "stale-reference")
        self.assertEqual(len(s.m.takes), 2)
        again = s.press(Press("forget", 154, source="cli", extra={"screen": "start"}))
        self.assertEqual((again["ignored"], s.fb(again)), ("not-learned", "⇥ start screen was not learned"))

    def test_learning_a_start_screen_supersedes_a_pending_one(self):
        s = both_learned()
        s.appear("end", 140)
        s.gone("end", 141)
        s.appear("start", 150)                                        # pending
        r = s.learn("start", 151)                                     # another start screen, now
        self.assertTrue(r["superseded_pending"])
        self.assertEqual(r["screen_effect"]["action"], "pending")


class NextEffectTest(unittest.TestCase):
    def test_the_rules_as_one_function(self):
        none = {"mode": "none", "undo": [], "next_take": 1, "implicit": True, "start_pending": False}
        opened = {"mode": "open", "take": 2, "undo": ["open"], "next_take": 3, "implicit": False}
        closed = {"mode": "closed", "take": 2, "undo": ["open", "close"], "next_take": 3, "implicit": False}
        rows = [(none, "end-screen", {"action": "close", "take": 1, "implicit": True}),
                (opened, "end-screen", {"action": "close", "take": 2}),
                (closed, "end-screen", {"action": "ignored", "reason": "no-take-open"}),
                (none, "start-screen", {"action": "pending", "take": 1}),
                (opened, "start-screen", {"action": "ignored", "reason": "take-already-open"}),
                (closed, "start-screen", {"action": "pending", "take": 3}),
                (closed, "start-screen-gone", {"action": "ignored", "reason": "not-pending"}),
                ({**closed, "start_pending": True}, "start-screen-gone", {"action": "open", "take": 3, "previous": 2}),
                ({**opened, "start_pending": True}, "start-screen-gone",
                 {"action": "ignored", "reason": "take-already-open"}),
                ({"mode": "none", "undo": [], "next_take": 1}, "end-screen",          # an older snapshot
                 {"action": "close", "take": 1, "implicit": True})]
        for state, kind, want in rows:
            with self.subTest(state=state, kind=kind):
                self.assertEqual(ev.next_effect(state, kind), want)
        self.assertEqual(ev.learn_effects(closed), {"end": {"action": "ignored", "reason": "no-take-open"},
                                                    "start": {"action": "pending", "take": 3}})


class HintEqualsEffectTest(unittest.TestCase):
    """The pill's learn hint is computed from the published state by the function
    the model decides with; held here against what a learn press then does."""

    def published(self, s, q):
        return json.loads(json.dumps(s.m.take_state(q)))

    def assert_learn_hint_matches(self, s: Auto, kind: str, q: float):
        state = self.published(s, q)
        predicted = ev.learn_effects(state)[kind]
        hinted = {h[0] for h in pill.learn_hints(state)}
        model = copy.deepcopy(s.m)
        rec = model.press(learn(kind, q), q)
        if not rec["accepted"]:
            return None
        got = rec["screen_effect"]
        self.assertEqual(got["action"], predicted["action"], (state, got))
        if predicted["action"] in ("close", "pending"):
            self.assertEqual(got.get("take"), predicted.get("take"))
        key = "learn_end" if kind == "end" else "learn_start"
        learned = (state.get("screens") or {}).get(kind, {}).get("learned")
        self.assertEqual(key in hinted, predicted["action"] in ("close", "pending") and not learned)
        return rec

    def test_every_rules_row(self):
        rows = {"no take yet": [], "take open": [("take", 105)], "take closed": [("take", 105), ("take", 110)],
                "after ⌫": [("take", 105), ("take", 110), ("correct", 113)]}
        for name, setup in rows.items():
            for kind in ("end", "start"):
                with self.subTest(row=name, screen=kind):
                    s = Auto()
                    for k, q in setup:
                        s.press(hk(k, q))
                    self.assertIsNotNone(self.assert_learn_hint_matches(s, kind, 120.0))

    def test_random_sequences_with_screens_pauses_and_corrections(self):
        rnd = random.Random(1008)
        checked = 0
        for case in range(500):
            s = Auto(debounce=1.0)
            q, seg = 100.0, 1
            present = {"start": False, "end": False}
            for _ in range(rnd.randint(1, 22)):
                q += rnd.uniform(0.1, 2.5)
                what = rnd.choice(["take", "correct", "learn", "learn", "screen", "screen", "screen", "pause",
                                   "forget"])
                if what == "pause":
                    if not s.m.paused:
                        s.pause_at(q, end_qpc=q + 0.3, duration=q + 0.3 - s.m.segments[seg]["epoch_qpc"],
                                   source="cli")
                    else:
                        seg += 1
                        s.resume_at(q, epoch=q + 1.0, source="cli")
                        q += 1.0
                    continue
                if what in ("take", "correct"):
                    s.press(Press(what, q, source=rnd.choice(["hotkey", "cli"])))
                    continue
                kind = rnd.choice(["start", "end"])
                if what == "forget":
                    s.press(Press("forget", q, source="cli", extra={"screen": kind}))
                    present[kind] = False
                    continue
                if what == "learn":
                    with self.subTest(case=case, at=q, screen=kind):
                        if self.assert_learn_hint_matches(s, kind, q) is not None:
                            checked += 1
                    if s.learn(kind, q)["accepted"]:
                        present[kind] = True
                    continue
                if s.m.references[kind] is None or s.m.paused:
                    continue
                state = self.published(s, q)
                change = "gone" if present[kind] else "appear"
                key = {"appear": ev.END_SCREEN if kind == "end" else ev.START_SCREEN,
                       "gone": ev.START_GONE}[change]
                r = s.press(seen(kind, change, q, s.ref[kind]))
                present[kind] = change == "appear"
                if kind == "end" and change == "gone":
                    self.assertEqual(r["action"], "gone")
                    continue
                predicted = ev.next_effect(state, key)
                with self.subTest(case=case, at=q, screen=kind, change=change):
                    self.assertEqual(r["screen_effect"]["action"] if r["screen_effect"]["action"] != "ignored"
                                     or r["ignored"] != "earlier-than-the-last-boundary" else "ignored",
                                     predicted["action"])
                    if predicted["action"] == "ignored":
                        self.assertEqual(r["screen_effect"]["reason"], predicted["reason"])
                    else:
                        self.assertEqual((r["action"], r["take"]), (predicted["action"], predicted["take"]))
                        text = s.fb(r)
                        self.assertIn(str(predicted["take"]) if predicted["action"] != "pending" else "opens", text)
                    checked += 1
            # whatever happened, the takes stay in order and never overlap
            summary = s.end(5.0) if not s.m.paused else s.m.finish()
            spans = [(k["segment"], k["start_s"], k["end_s"]) for k in summary["kept"]]
            self.assertEqual(spans, sorted(spans), (case, spans))
        self.assertGreater(checked, 1000)


class PillTest(unittest.TestCase):
    def lines(self, s, q, status="recording", elapsed=0):
        return pill.pill_lines(status, elapsed, json.loads(json.dumps(s.m.take_state(q))), LABELS).split("\n")

    def test_the_hint_names_the_learn_key_that_would_make_a_boundary(self):
        s = Auto()
        self.assertEqual(self.lines(s, 141)[1], "T start take · P pause · M mark · ] end")
        s.learn("end", 110)                              # implicit closed: now a start screen would open one
        s.gone("end", 111)
        self.assertEqual(self.lines(s, 112)[1], "T next take · ⌫ move close here · [ start")
        s.learn("start", 113)
        s.gone("start", 114)                             # take 2 open; both learned: no learn key
        self.assertEqual(self.lines(s, 115)[1], "T close · ⌫ restart take · P pause")

    def test_the_long_words_when_they_fit(self):
        labels = {**LABELS, "take": "T", "correct": "B"}
        s = Auto()
        s.press(hk("take", 105))
        s.press(hk("take", 110))
        s.press(hk("correct", 113))                      # "T next take · B drop take 1, restart" (36)
        state = json.loads(json.dumps(s.m.take_state(114)))
        self.assertEqual(pill.hint_line("recording", state, labels), "T next take · B drop take 1, restart")
        short = {k: v for k, v in labels.items()}
        s2 = Auto()
        s2.learn("end", 105)
        s2.gone("end", 106)
        state2 = json.loads(json.dumps(s2.m.take_state(107)))
        state2["undo"] = []                              # a state with no correction: room for the long words
        self.assertTrue(pill.hint_line("recording", state2, short).endswith("· [ start screen"))

    def test_paused_shows_no_learn_key(self):
        s = Auto()
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        self.assertEqual(self.lines(s, 120, "paused")[1], "P resume · nothing is recording")

    def test_an_old_agent_config_without_learn_keys_shows_none(self):
        class Old:
            take_hotkey, correct_hotkey, pause_hotkey, mark_hotkey = "Ctrl+Alt+T", "Ctrl+Alt+Backspace", \
                "Ctrl+Alt+P", "Ctrl+Alt+M"
        s = Auto()
        hint = pill.hint_line("recording", json.loads(json.dumps(s.m.take_state(110))), pill.key_labels(Old()))
        self.assertEqual(hint, "T start take · P pause · M mark")

    def test_feedback_lines_fit_the_pill(self):
        s = both_learned()
        texts = [s.fb(s.appear("end", 140)), s.fb(s.gone("end", 141)), s.fb(s.appear("start", 150)),
                 s.fb(s.gone("start", 151)), s.fb(s.learn("end", 160)), s.fb(s.learn("start", 170, stable=False))]
        self.assertEqual(texts, ["◇ end screen → take 2 closed", None, "◇ start screen up → take opens when it goes",
                                 "◇ start screen gone → take 3 opened", "⇥ end screen re-learned · take 3 closed",
                                 "⇥ start screen re-learned · opens a take when gone (moving)"])
        for t in filter(None, texts):
            self.assertLessEqual(len(t), 60, t)


class RecordTest(unittest.TestCase):
    def test_no_learning_no_block(self):
        s = Session()
        s.press(hk("take", 105))
        self.assertIsNone(s.m.auto_takes_record())
        self.assertNotIn("implicit", s.m.takes[0])

    def test_the_sidecar_block_keeps_every_reference(self):
        s = both_learned()
        s.learn("end", 130)
        rec = s.m.auto_takes_record()
        self.assertEqual(rec["rule"], "auto-takes/1")
        self.assertEqual([r["screen"] for r in rec["references"]], ["end", "start", "end"])
        self.assertEqual(rec["current"], {"start": s.ref["start"], "end": s.ref["end"]})
        self.assertEqual(rec["references"][0]["thumb"], THUMB)
        self.assertEqual(rec["references"][0]["size"], [16, 10])
        json.dumps(rec)

    def test_active_json_block_for_c1e(self):
        s = both_learned()
        s.appear("end", 140)
        st = s.m.auto_takes_state()
        self.assertEqual(set(st), {"start", "end"})
        end = st["end"]
        self.assertEqual((end["learned"], end["present"], end["seen"], end["last_seen_qpc"]), (True, True, 2, 140))
        self.assertEqual(end["last_effect"]["action"], "close")
        self.assertEqual(end["last_learn"]["outcome"], "learned")
        self.assertFalse(st["start"]["pending"])
        self.assertEqual(s.m.take_state(141)["screens"],
                         {"start": {"learned": True, "present": False}, "end": {"learned": True, "present": True}})


if __name__ == "__main__":
    unittest.main()
