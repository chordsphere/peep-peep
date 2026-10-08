"""Session C1a: the event state machine (events.py), every press sequence the
brief names, the placement of presses on the segment timeline, and the
kept / total computation. Pure: no files, no processes, no clock. (C1c's
correction chart, row by row, is tests/test_correction.py.)"""

import unittest

from peep import events as ev
from peep.events import EventModel, Press


def hk(kind, qpc):
    return Press(kind, qpc, source="hotkey")


def cli(kind, qpc):
    return Press(kind, qpc, source="cli")


class Session:
    """A model with segment 1 live from qpc 100 (its first frame), launch at 99.5."""

    def __init__(self, debounce=1.0):
        self.m = EventModel(debounce)
        self.m.segment_started(1, 100.0, 99.5)

    def press(self, p):
        return self.m.press(p, p.qpc if p.qpc is not None else 0.0)

    def pause_at(self, qpc, end_qpc, duration, pause_kind="pause-toggle", source="hotkey"):
        r = self.press(Press(pause_kind, qpc, source=source))
        assert r["action"] == "pause", r
        k = self.m.current_segment
        self.m.segment_ended(k, end_qpc, duration)
        self.m.set_paused(True)
        return r

    def resume_at(self, qpc, epoch, launch=None, kind="pause-toggle", source="hotkey"):
        r = self.press(Press(kind, qpc, source=source))
        assert r["action"] == "resume", r
        k = self.m.current_segment + 1
        self.m.segment_started(k, epoch, launch if launch is not None else epoch - 0.5)
        self.m.set_paused(False)
        return r

    def end(self, duration, end_qpc=None):
        self.m.segment_ended(self.m.current_segment, end_qpc, duration)
        return self.m.finish()


class TakeToggleTest(unittest.TestCase):
    def test_first_press_opens_next_closes(self):
        s = Session()
        a = s.press(hk("take", 105))
        b = s.press(hk("take", 110))
        self.assertEqual((a["action"], a["take"]), ("open", 1))
        self.assertEqual((b["action"], b["take"]), ("close", 1))
        self.assertEqual(a["media_s"], 5.0)
        self.assertEqual(a["since_ffmpeg_start_s"], 5.5)            # from the ffmpeg launch, like marks
        summary = s.end(30.0)
        self.assertEqual((summary["takes"], summary["kept_s"], summary["total_s"]), (1, 5.0, 30.0))
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 5.0, "end_s": 10.0, "take": 1}])
        self.assertFalse(summary["whole"])

    def test_zero_take_presses_keeps_the_whole_session(self):
        s = Session()
        summary = s.end(42.5)
        self.assertTrue(summary["whole"])
        self.assertEqual((summary["kept_s"], summary["total_s"], summary["takes"]), (42.5, 42.5, 0))
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 0.0, "end_s": 42.5, "take": None}])

    def test_take_still_open_at_the_end_closes_with_the_session(self):
        s = Session()
        s.press(hk("take", 120))
        summary = s.end(30.0)
        take = s.m.takes[0]
        self.assertEqual(take["close"]["reason"], "session-end")
        self.assertEqual(take["status"], "kept")
        self.assertEqual(summary["kept_s"], 10.0)

    def test_several_takes_add_up(self):
        s = Session()
        for q in (102, 104, 110, 115, 120, 121):
            s.press(hk("take", q))
        summary = s.end(30.0)
        self.assertEqual(summary["takes"], 3)
        self.assertEqual(summary["kept_s"], 2 + 5 + 1)

    def test_press_before_the_first_frame_lands_on_it(self):
        s = Session()
        r = s.press(hk("take", 99.8))           # pressed while segment 1 was starting
        self.assertEqual((r["segment"], r["media_s"], r["before_first_frame"]), (1, 0.0, True))


class RetakeAliasTest(unittest.TestCase):
    """C1c replaced C1a's retake rule with the correction chart (every row:
    tests/test_correction.py). `retake` stays an accepted name for it."""

    def test_retake_is_the_correction_key(self):
        s = Session()
        s.press(hk("take", 105))
        r = s.press(hk("retake", 112))
        self.assertEqual((r["kind"], r["requested_as"], r["action"]), ("correct", "retake", "dropped-take"))
        self.assertEqual((r["take"], r["discarded_take"]), (2, 1))
        s.press(hk("take", 120))
        summary = s.end(30.0)
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 12.0, "end_s": 20.0, "take": 2}])

    def test_correction_right_after_closing_a_take_is_debounced(self):
        """The chart: a correction within 1 s of the previous accepted press is
        ignored, whatever that press was (C1a's retake was per chord)."""
        s = Session()
        s.press(hk("take", 105))
        s.press(hk("take", 110))
        r = s.press(hk("correct", 110.2))
        self.assertEqual((r["accepted"], r["ignored"], r["debounce"]["after"]), (False, "debounce", "take"))
        self.assertEqual(s.m.takes[0]["close"]["media_s"], 10.0)            # the close did not move


class DebounceTest(unittest.TestCase):
    def test_second_press_inside_the_window_is_ignored_and_recorded(self):
        s = Session(debounce=1.0)
        s.press(hk("take", 105))
        r = s.press(hk("take", 105.4))
        self.assertFalse(r["accepted"])
        self.assertEqual(r["ignored"], "debounce")
        self.assertEqual(r["debounce"], {"since_last_s": 0.4, "window_s": 1.0})
        self.assertIsNotNone(s.m.open_take())                   # still open: the bounce did not close it
        self.assertEqual(len(s.m.events), 2)                   # the ignored press is in the record

    def test_a_deliberate_second_correction_after_the_window_is_honoured(self):
        s = Session(debounce=1.0)
        s.press(hk("take", 101))
        s.press(hk("correct", 105))
        ignored = s.press(hk("correct", 105.9))
        honoured = s.press(hk("correct", 106.1))
        self.assertEqual(ignored["ignored"], "debounce")
        self.assertTrue(honoured["accepted"])
        self.assertEqual(honoured["discarded_take"], 2)

    def test_window_counts_from_the_last_accepted_press(self):
        s = Session(debounce=1.0)
        s.press(hk("take", 100))
        s.press(hk("take", 100.6))            # ignored
        r = s.press(hk("take", 101.1))        # 1.1 s after the accepted one: honoured
        self.assertTrue(r["accepted"])

    def test_wsl_commands_are_never_debounced(self):
        s = Session(debounce=1.0)
        a = s.press(cli("take", 105))
        b = s.press(cli("take", 105.1))
        self.assertEqual((a["action"], b["action"]), ("open", "close"))

    def test_pause_chord_is_debounced_too(self):
        s = Session(debounce=1.0)
        s.press(hk("pause-toggle", 110))
        r = s.press(hk("pause-toggle", 110.3))
        self.assertEqual(r["ignored"], "debounce")

    def test_zero_debounce_accepts_everything(self):
        s = Session(debounce=0.0)
        s.press(hk("take", 105))
        self.assertTrue(s.press(hk("take", 105.0))["accepted"])


class PauseTest(unittest.TestCase):
    def test_pause_inside_a_take_carries_it_across(self):
        s = Session()
        s.press(hk("take", 105))                                   # take 1 opens at 5 s
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        s.resume_at(200, epoch=300.0)                              # segment 2 first frame at qpc 300
        self.assertIsNotNone(s.m.open_take())                      # still open after the resume
        close = s.press(hk("take", 304))
        self.assertEqual((close["segment"], close["media_s"]), (2, 4.0))
        summary = s.end(20.0)
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 5.0, "end_s": 10.5, "take": 1},
                                           {"segment": 2, "start_s": 0.0, "end_s": 4.0, "take": 1}])
        self.assertEqual((summary["segments"], summary["total_s"], summary["kept_s"]), (2, 30.5, 9.5))

    def test_presses_while_paused_take_effect_at_the_boundary(self):
        s = Session()
        s.press(hk("take", 105))
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        close = s.press(cli("take", 150))                          # closed during the pause
        self.assertEqual((close["segment"], close["after_segment"]), (None, 1))
        opened = s.press(cli("take", 160))                         # opened during the pause
        s.resume_at(200, epoch=300.0)
        summary = s.end(20.0)
        # the close sits at the end of segment 1, the open at the start of segment 2
        self.assertEqual(summary["kept"], [{"segment": 1, "start_s": 5.0, "end_s": 10.5, "take": 1},
                                           {"segment": 2, "start_s": 0.0, "end_s": 20.0, "take": 2}])
        self.assertEqual(opened["action"], "open")

    def test_a_dropped_take_while_paused_restarts_at_the_next_segment(self):
        s = Session()
        s.press(hk("take", 105))
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        r = s.press(cli("correct", 150))
        s.resume_at(200, epoch=300.0)
        summary = s.end(5.0)
        self.assertEqual(r["discarded_take"], 1)
        self.assertEqual(summary["kept"], [{"segment": 2, "start_s": 0.0, "end_s": 5.0, "take": 2}])

    def test_a_press_made_while_resuming_lands_at_the_new_segment_start(self):
        s = Session()
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        s.resume_at(200, epoch=300.0)
        r = s.press(hk("take", 250))      # pressed after the resume, before segment 2's first frame
        self.assertEqual((r["segment"], r["after_segment"]), (None, 1))
        summary = s.end(5.0)
        self.assertEqual(summary["kept"], [{"segment": 2, "start_s": 0.0, "end_s": 5.0, "take": 1}])

    def test_pause_and_resume_validity(self):
        s = Session()
        self.assertEqual(s.press(cli("resume", 101))["ignored"], "not-paused")
        s.pause_at(110, end_qpc=110.5, duration=10.5, pause_kind="pause", source="cli")
        self.assertEqual(s.press(cli("pause", 120))["ignored"], "already-paused")
        self.assertEqual(s.press(cli("pause-toggle", 130))["action"], "resume")

    def test_pause_record(self):
        s = Session()
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        s.resume_at(200, epoch=300.0)
        s.m.note_pause_capture(1, 110.5, 300.0)
        [p] = s.m.pauses
        self.assertEqual((p["after_segment"], p["pause_event"], p["resume_event"]), (1, 1, 2))
        self.assertEqual(p["seconds"], 189.5)

    def test_pause_with_no_take_renders_every_segment_whole(self):
        s = Session()
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        s.resume_at(200, epoch=300.0)
        summary = s.end(4.5)
        self.assertTrue(summary["whole"])
        self.assertEqual(summary["kept_s"], 15.0)
        self.assertEqual([k["segment"] for k in summary["kept"]], [1, 2])

    def test_segment_end_comes_from_the_media_duration(self):
        s = Session()
        s.m.segment_ended(1, 999.0, 12.0)            # the q-sent stamp is overridden by epoch + duration
        self.assertEqual(s.m.segments[1]["end_qpc"], 112.0)
        self.assertEqual(s.m.locate(113.0)["after_segment"], 1)
        self.assertEqual(s.m.locate(111.0)["media_s"], 11.0)


class CancelledPauseTest(unittest.TestCase):
    def test_a_pause_that_never_took_effect_leaves_no_pause(self):
        s = Session()
        r = s.press(hk("pause-toggle", 110))
        self.assertEqual(len(s.m.pauses), 1)
        self.assertEqual(s.m.cancel_pending_pause("superseded-by-stop"), 1)    # a stop won
        self.assertEqual(s.m.pauses, [])
        self.assertEqual((r["accepted"], r["ignored"], r["action"]), (False, "superseded-by-stop", None))

    def test_a_pause_that_happened_is_never_cancelled(self):
        s = Session()
        s.pause_at(110, end_qpc=110.5, duration=10.5)
        self.assertEqual(s.m.cancel_pending_pause("recording-ended"), 0)        # ended while paused: real
        self.assertEqual(len(s.m.pauses), 1)


class MarkAndRecordShapeTest(unittest.TestCase):
    def test_marks_are_events_and_never_cuts(self):
        s = Session()
        r = s.press(Press("mark", 104, source="cli", label="intro"))
        self.assertEqual((r["accepted"], r["action"], r["label"]), (True, "mark", "intro"))
        self.assertTrue(s.end(10.0)["whole"])

    def test_record_carries_what_c1b_needs(self):
        s = Session()
        r = s.m.press(Press("take", 105.25, at="2026-10-07T10:00:00.000-04:00", source="hotkey"), 105.3)
        for key in ("id", "kind", "source", "requested_at", "qpc", "consumed_qpc", "segment", "media_s",
                    "since_ffmpeg_start_s", "accepted", "action", "take", "discarded_take", "fiducial"):
            self.assertIn(key, r)
        self.assertEqual(r["qpc_from"], "press")
        old = s.m.press(Press("take", None), 106.0)                # an old request without a stamp
        self.assertEqual((old["qpc"], old["qpc_from"]), (106.0, "consumed"))

    def test_unknown_kind_is_recorded_not_raised(self):
        s = Session()
        self.assertEqual(s.press(cli("rewind", 101))["ignored"], "unknown-kind")

    def test_from_request(self):
        p = Press.from_request({"kind": "retake", "requested_qpc": 12.5, "requested_at": "t", "source": "hotkey"})
        self.assertEqual(p, Press("correct", 12.5, "t", "hotkey", "", "retake"))      # C1c: the old name
        p = Press.from_request({"kind": "correct", "requested_qpc": 12.5, "requested_at": "t", "source": "cli"})
        self.assertEqual((p.kind, p.requested_as), ("correct", ""))
        mark = Press.from_request({"requested_at": "t", "source": "cli", "label": "x"})       # B's mark file
        self.assertEqual((mark.kind, mark.qpc, mark.label), ("mark", None, "x"))

    def test_counts_for_the_pill(self):
        s = Session()
        self.assertEqual(s.m.counts(), {"takes": 0, "open": False})
        s.press(hk("take", 101))
        self.assertEqual(s.m.counts(), {"takes": 1, "open": True})
        s.press(hk("correct", 103))
        self.assertEqual(s.m.counts(), {"takes": 1, "open": True})      # one dropped, a fresh one open
        s.press(hk("take", 105))
        self.assertEqual(s.m.counts(), {"takes": 1, "open": False})


class SummaryLineTest(unittest.TestCase):
    def test_brief_example(self):
        summary = {"segments": 2, "takes": 3, "kept_s": 134, "total_s": 302, "whole": False}
        self.assertEqual(ev.summary_line(summary), "3 takes  ·  02:14 kept of 05:02  ·  2 segments")

    def test_plain_recording_adds_nothing(self):
        self.assertIsNone(ev.summary_line({"segments": 1, "takes": 0, "kept_s": 9, "total_s": 9, "whole": True}))
        self.assertIsNone(ev.summary_line(None))
        self.assertEqual(ev.summary_line({"segments": 3, "takes": 0, "kept_s": 9, "total_s": 9, "whole": True}),
                         "3 segments")
        self.assertEqual(ev.summary_line({"segments": 1, "takes": 1, "kept_s": 3725, "total_s": 4000,
                                          "whole": False}), "1 take  ·  1:02:05 kept of 1:06:40")

    def test_resolve_position_edges(self):
        d = {1: 10.0, 2: 5.0}
        self.assertEqual(ev.resolve_position({"segment": 1, "media_s": 99}, d, True), (1, 10.0))
        self.assertEqual(ev.resolve_position({"segment": 2, "media_s": -3}, d, False), (2, 0.0))
        self.assertEqual(ev.resolve_position({"segment": None, "after_segment": 2}, d, True), (2, 5.0))
        self.assertEqual(ev.resolve_position({"segment": None, "after_segment": 0}, d, True), (1, 0.0))


if __name__ == "__main__":
    unittest.main()
