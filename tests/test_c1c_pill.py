"""Session C1c: the two-line pill. The ratified mockups reproduced from real
event-model states (C1d: with the learn key that fits at the end of the hint,
`] end` / `[ start`, from the same decision function), key labels from the configured chords, the 1.5 s
feedback line through the agent, `pill_hints`, the enlarged pill's corner
against the fiducial patch, and the renamed config keys (`correct_*`, with
`retake_*` accepted) in the loader and in `peep config`."""

import datetime as dt
import json
import unittest

from peep import config as c, configedit, events as ev, pill
from peep.flash import patch_corner, patch_rect
from tests import TempDirMixin
from tests.test_agent import AgentHarness
from tests.test_events import Session, hk

LABELS = pill.key_labels(c.AgentConfig())


def published(s: Session, at: float) -> dict:
    """The take state as the recorder publishes it (active.json, JSON round trip)."""
    return json.loads(json.dumps(s.m.take_state(at)))


class MockupTest(unittest.TestCase):
    """The mockups the architect ratified, verbatim, from the states that produce them:
    take 1 runs 1:18, take 2 opens at 2:24 (segment 1's first frame at qpc 100)."""

    def lines(self, status, elapsed, state, feedback=None, hints=True):
        return pill.pill_lines(status, elapsed, state, LABELS, hints=hints, feedback=feedback).split("\n")

    def two_takes(self) -> Session:
        s = Session()
        s.press(hk("take", 110))
        s.press(hk("take", 188))
        s.press(hk("take", 244))
        return s

    def test_no_takes_yet(self):
        s = Session()
        self.assertEqual(self.lines("recording", 41, published(s, 141)),
                         ["● 00:41  whole video kept", "T start take · P pause · M mark · ] end"])

    def test_take_open(self):
        s = self.two_takes()
        self.assertEqual(self.lines("recording", 192, published(s, 292)),
                         ["● 03:12  ◉ take 2 · 0:48", "T close · ⌫ restart take · P pause · ] end"])

    def test_take_just_closed(self):
        s = self.two_takes()
        s.press(hk("take", 300))
        self.assertEqual(self.lines("recording", 200, published(s, 300)),
                         ["● 03:20  ○ 2 takes · 2:14 kept", "T next take · ⌫ move close here · [ start"])

    def test_after_one_correction(self):
        s = self.two_takes()
        s.press(hk("take", 300))
        r = s.press(hk("correct", 304.2))
        self.assertEqual(self.lines("recording", 204.2, published(s, 304.2)),
                         ["● 03:24  ○ 2 takes · 2:18 kept", "T next take · ⌫ drop take 2, restart"])
        self.assertEqual(self.lines("recording", 204.2, published(s, 304.2), ev.press_feedback(r, 5)),
                         ["● 03:24  ○ 2 takes · 2:18 kept", "⌫ close moved +4.2 s"])

    def test_paused(self):
        s = self.two_takes()
        s.press(hk("take", 300))
        s.press(hk("correct", 304.2))
        s.pause_at(305, end_qpc=305.1, duration=205.1)
        self.assertEqual(self.lines("paused", 204.2, published(s, 330)),
                         ["❚❚ PAUSED 03:24  ○ 2 takes", "P resume · nothing is recording"])

    def test_feedback_lines(self):
        s = self.two_takes()
        s.press(hk("take", 300))
        s.press(hk("correct", 304.2))
        dropped = s.press(hk("correct", 310))
        fast = s.press(hk("correct", 310.5))
        empty = Session().press(hk("correct", 101))
        texts = [pill.feedback_text(ev.press_feedback(r, n), LABELS) for n, r in enumerate((dropped, fast, empty))]
        self.assertEqual(texts, ["⌫ take 2 dropped · take 3 started", "⌫ ignored — too fast", "⌫ nothing to correct"])

    def test_the_other_keys_feedback(self):
        s = Session()
        opened, closed = s.press(hk("take", 105)), s.press(hk("take", 110))
        mark = s.press(hk("mark", 111))
        bounce = s.press(hk("take", 110.3))
        self.assertEqual([pill.feedback_text(ev.press_feedback(r, 1, mark=2 if r is mark else None), LABELS)
                          for r in (opened, closed, mark, bounce)],
                         ["T take 1 started", "T take 1 closed", "M mark 2", "T ignored — too fast"])
        s.pause_at(112, end_qpc=112.1, duration=12.1, source="cli")
        late = s.press(ev.Press("take", 130, source="cli"))                 # pressed while paused
        self.assertEqual(pill.feedback_text(ev.press_feedback(late, 2), LABELS), "T take 2 started · at the resume")

    def test_pause_and_resume_presses_get_feedback_too(self):
        s = Session()
        paused = s.pause_at(110, end_qpc=110.2, duration=10.2)
        resumed = s.resume_at(130, epoch=131.0)
        self.assertEqual([pill.feedback_text(ev.press_feedback(r, 1), LABELS) for r in (paused, resumed)],
                         ["P pausing…", "P resuming…"])
        self.assertEqual(pill.pill_lines("pausing", None, published(s, 110.1), LABELS,
                                         feedback=ev.press_feedback(paused, 1)), "❚❚ pausing…\nP pausing…")
        # once the pause has happened the feedback has nothing left to say: the hint is back
        self.assertEqual(pill.pill_lines("paused", 10, published(s, 120), LABELS,
                                         feedback=ev.press_feedback(paused, 1)),
                         "❚❚ PAUSED 00:10\nP resume · nothing is recording")

    def test_hints_off_is_one_line_but_feedback_still_shows(self):
        s = Session()
        r = s.press(hk("take", 105))
        state = published(s, 106)
        self.assertEqual(self.lines("recording", 6, state, hints=False), ["● 00:06  ◉ take 1 · 0:01"])
        self.assertEqual(self.lines("recording", 6, state, ev.press_feedback(r, 1), hints=False),
                         ["● 00:06  ◉ take 1 · 0:01", "T take 1 started"])

    def test_transitional_states_have_no_hint(self):
        for status, text in (("starting", "● starting…"), ("stopping", "■ saving…"), ("pausing", "❚❚ pausing…"),
                             ("resuming", "● resuming…")):
            self.assertEqual(pill.pill_lines(status, None, published(Session(), 101), LABELS), text)


class KeyLabelTest(unittest.TestCase):
    def test_labels_come_from_the_configured_chords(self):
        self.assertEqual(LABELS, {"take": "T", "correct": "⌫", "pause": "P", "mark": "M", "learn_start": "[",
                                  "learn_end": "]"})                 # C1d added the learn keys
        ag = c.from_mapping({"agent": {"take_hotkey": "F13", "correct_hotkey": "Ctrl+Alt+Num2"}}).agent
        labels = pill.key_labels(ag)
        self.assertEqual((labels["take"], labels["correct"]), ("F13", "Num2"))
        s = Session()
        s.press(hk("take", 105))
        self.assertEqual(pill.hint_line("recording", published(s, 106), labels), "F13 close · Num2 restart take · P pause")

    def test_keys_that_abbreviate_alike_are_shown_in_full(self):
        ag = c.from_mapping({"agent": {"take_hotkey": "Ctrl+Alt+T", "correct_hotkey": "Ctrl+Shift+T"}}).agent
        labels = pill.key_labels(ag)
        self.assertEqual((labels["take"], labels["correct"]), ("Ctrl+Alt+T", "Ctrl+Shift+T"))

    def test_a_long_hint_is_abbreviated_to_fit(self):
        ag = c.from_mapping({"agent": {"take_hotkey": "Ctrl+Alt+T", "correct_hotkey": "Ctrl+Shift+T"}}).agent
        labels = pill.key_labels(ag)
        s = Session()
        s.press(hk("take", 105))
        hint = pill.hint_line("recording", published(s, 106), labels)
        self.assertLessEqual(len(hint), pill.HINT_MAX_CHARS)
        self.assertTrue(hint.startswith("Ctrl+Alt+T close · Ctrl+Shift+T restart"), hint)
        self.assertNotIn("pause", hint)                         # the trailing key went first

    def test_short_times(self):
        self.assertEqual([pill.format_short(x) for x in (0, 48, 134.9, 3725, None)],
                         ["0:00", "0:48", "2:14", "1:02:05", "0:00"])


class LiveGrowthTest(unittest.TestCase):
    def test_an_open_take_grows_between_presses_and_a_closed_one_does_not(self):
        s = Session()
        s.press(hk("take", 105))
        st = published(s, 106)
        grown = pill.live_take_state(st, 10.0, recording=True)
        self.assertEqual((grown["take_s"], grown["kept_s"]), (11.0, 11.0))
        self.assertEqual(pill.live_take_state(st, 10.0, recording=False)["take_s"], 1.0)     # paused: still
        s.press(hk("take", 110))
        closed = pill.live_take_state(published(s, 110), 30.0, recording=True)
        self.assertEqual((closed["take_s"], closed["kept_s"]), (5.0, 5.0))
        self.assertIsNone(pill.live_take_state(None, 1.0, True))


class PillCornerTest(unittest.TestCase):
    """The pill grew to two lines: wherever it sits, it stays flush in its corner
    and clear of the fiducial patch, which C1a puts in the opposite corner."""

    def test_two_line_pill_never_meets_the_patch(self):
        for sw, sh in ((2560, 1600), (1920, 1200), (1280, 800)):
            for position in c.PILL_POSITIONS:
                for w, h in ((360, 34), (520, 70), (900, 200)):
                    with self.subTest(screen=(sw, sh), position=position, size=(w, h)):
                        x, y = pill.overlay_position(position, sw, sh, w, h, 24)
                        px, py, pw, ph = patch_rect(patch_corner("auto", position), 200, sw, sh)
                        overlap = x < px + pw and px < x + w and y < py + ph and py < y + h
                        self.assertFalse(overlap)
                        vertical, _, horizontal = position.partition("-")
                        self.assertEqual(y if vertical == "top" else sh - (y + h), 24)    # flush in its corner
                        if horizontal == "right":
                            self.assertEqual(sw - (x + w), 24)


class AgentPillTest(AgentHarness, unittest.TestCase):
    """The agent's tick: the state line and hint from active.json's take_state, a
    press's feedback in place of the hint for 1.5 s, then the hint again."""

    def claim(self, s: Session, at: float, now: dt.datetime, **extra):
        state = published(s, at)
        state["at"] = now.isoformat()
        self.control.claim({"uid": "u", "origin": "terminal", "status": "recording", "final": "f",
                            "captured_s": 0.0, "recording_since": (now - dt.timedelta(seconds=at - 100)).isoformat(),
                            "takes": state["takes"], "take_open": state["mode"] == "open", "take_state": state,
                            **extra})

    def test_feedback_replaces_the_hint_for_1_5_s(self):
        a = self.make_agent()
        now = dt.datetime.now().astimezone()
        a.now = lambda: now
        s = Session()
        s.press(hk("take", 110))
        r = s.press(hk("take", 188))
        self.claim(s, 188, now, feedback=ev.press_feedback(r, 2))
        a.tick()
        self.assertEqual(self.ui.pills[-1], "● 01:28  ○ 1 take · 1:18 kept\nT take 1 closed")
        a.now = lambda: now + dt.timedelta(seconds=1.4)
        a.tick()
        self.assertTrue(self.ui.pills[-1].endswith("\nT take 1 closed"), self.ui.pills[-1])
        a.now = lambda: now + dt.timedelta(seconds=1.6)
        a.tick()
        self.assertTrue(self.ui.pills[-1].endswith("\nT next take · ⌫ move close here · [ start"), self.ui.pills[-1])
        # the same press is not shown again; the next one is
        r2 = s.press(hk("correct", 192))
        self.control.update_active(feedback=ev.press_feedback(r2, 3), take_state={**published(s, 192),
                                                                                  "at": now.isoformat()})
        a.tick()
        self.assertTrue(self.ui.pills[-1].endswith("\n⌫ close moved +4.0 s"), self.ui.pills[-1])
        self.control.release()

    def test_feedback_older_than_its_display_time_is_not_replayed(self):
        """An agent started mid-recording (peep agent restart) finds the last press's
        feedback in active.json; minutes old, it is not shown as if it were new."""
        a = self.make_agent()
        now = dt.datetime.now().astimezone()
        a.now = lambda: now
        s = Session()
        r = s.press(hk("take", 110))
        fb = {**ev.press_feedback(r, 1), "at": (now - dt.timedelta(minutes=5)).isoformat()}
        self.claim(s, 110, now, feedback=fb)
        a.tick()
        self.assertEqual(self.ui.pills[-1], "● 00:10  ◉ take 1 · 0:00\nT close · ⌫ restart take · P pause · ] end")
        fresh = s.press(hk("take", 112))
        self.control.update_active(feedback={**ev.press_feedback(fresh, 2), "at": now.isoformat()},
                                   take_state={**published(s, 112), "at": now.isoformat()})
        a.tick()
        self.assertTrue(self.ui.pills[-1].endswith("\nT take 1 closed"), self.ui.pills[-1])
        self.control.release()

    def test_an_open_take_counts_up_between_presses(self):
        a = self.make_agent()
        now = dt.datetime.now().astimezone()
        s = Session()
        s.press(hk("take", 110))
        self.claim(s, 110, now)
        a.now = lambda: now + dt.timedelta(seconds=12)
        a.tick()
        self.assertEqual(self.ui.pills[-1], "● 00:22  ◉ take 1 · 0:12\nT close · ⌫ restart take · P pause · ] end")
        self.control.release()

    def test_pill_hints_off(self):
        cfg = c.from_mapping({"root": str(self.root), "agent": {"pill_hints": False}})
        a = self.make_agent(cfg=cfg)
        now = dt.datetime.now().astimezone()
        a.now = lambda: now
        s = Session()
        r = s.press(hk("correct", 105))
        self.claim(s, 105, now, feedback=ev.press_feedback(r, 1))
        a.tick()
        self.assertEqual(self.ui.pills[-1], "● 00:05  whole video kept\n⌫ nothing to correct")
        a.now = lambda: now + dt.timedelta(seconds=2)
        a.tick()
        self.assertEqual(self.ui.pills[-1], "● 00:07  whole video kept")
        self.control.release()


class RenamedConfigTest(TempDirMixin, unittest.TestCase):
    def test_defaults_and_the_old_names(self):
        d = c.Config()
        self.assertEqual((d.agent.correct_hotkey, d.flash.correct_color, d.agent.pill_hints),
                         ("Ctrl+Alt+Backspace", "#FFFF00", True))
        cfg = c.from_mapping({"agent": {"retake_hotkey": "Ctrl+Alt+Y"}, "flash": {"retake_color": "#FF8000"}})
        self.assertEqual((cfg.agent.correct_hotkey, cfg.flash.correct_color), ("Ctrl+Alt+Y", "#FF8000"))
        with self.assertRaisesRegex(c.ConfigError, "both correct_hotkey and retake_hotkey"):
            c.from_mapping({"agent": {"retake_hotkey": "Ctrl+Alt+Y", "correct_hotkey": "Ctrl+Alt+Z"}})
        with self.assertRaisesRegex(c.ConfigError, "agent.pill_hints must be bool"):
            c.from_mapping({"agent": {"pill_hints": "no"}})

    def test_peep_config_takes_the_old_name_and_writes_the_new(self):
        path = self.tmp / "config.toml"
        path.write_text('[agent]\nretake_hotkey = "Ctrl+Alt+Y"   # mine\n', encoding="utf-8")
        self.assertEqual(configedit.file_keys(path), {"agent.correct_hotkey"})
        res = configedit.set_value(path, "agent.retake_hotkey", "Ctrl+Alt+Z", environ={})
        self.assertEqual((res.key, res.old, res.new), ("agent.retake_hotkey", "Ctrl+Alt+Y", "Ctrl+Alt+Z"))
        # rewritten under the new name, its comment kept at its column
        self.assertEqual(path.read_text(encoding="utf-8"), '[agent]\ncorrect_hotkey = "Ctrl+Alt+Z"  # mine\n')
        res = configedit.unset_value(path, "agent.correct_hotkey", environ={})
        self.assertEqual(res.new, "Ctrl+Alt+Backspace")
        self.assertEqual(configedit.lookup("flash.retake_color").dotted, "flash.correct_color")
        rows = configedit.get_rows(c.Config(), path, "agent.retake_hotkey")
        self.assertEqual(rows, [("agent.correct_hotkey", '"Ctrl+Alt+Backspace"', "default")])


if __name__ == "__main__":
    unittest.main()
