"""Session C1d in the recorder, across the real process boundary to
fake_ffmpeg.py: the agent's learn and screen requests decided by the event
model, no corner patch for any of them, the learned screen's PNG under the
state folder (and gone with the recording), the sidecar's auto_takes block,
and active.json's auto_takes block for C1e."""

import json
import time
import unittest

from peep import screens
from tests.test_segments import SegmentHarness

W, H = 16, 10
END = bytes((i * 7) % 256 for i in range(W * H))


def learn_extra(kind, ref, thumb=END):
    return {"screen": kind, "ref": ref, "thumb": thumb.hex(), "size": [W, H], "stable": True, "waited_s": 0.1,
            "thresholds": screens.Thresholds().to_dict(), "same_as_current": False,
            "sampler": {"hz": 4.0, "thumb_width": 16}}


class AutoTakesRecorderTest(SegmentHarness, unittest.TestCase):
    def test_learn_and_visual_events_through_the_recorder(self):
        seen = {}

        def script(h):
            h.control.request_event("take", "cli")
            h.wait_consumed()
            h.control.request_event("learn", "hotkey", extra=learn_extra("end", "end-1"))   # closes take 1
            h.wait_consumed()
            seen["learned"] = dict(h.control.read_active())
            seen["png"] = sorted(p.name for p in (h.control.dir / "auto-takes").iterdir())
            h.control.request_event("screen", "visual", qpc=time.perf_counter(),
                                    extra={"screen": "end", "ref": "end-1", "change": "gone",
                                           "score": {"mad": 40.0, "changed_pct": 60.0}, "samples": 2})
            h.wait_consumed()
            seen["gone"] = dict(h.control.read_active())
            h.control.request_event("screen", "visual", qpc=time.perf_counter(),       # a replaced reference's
                                    extra={"screen": "end", "ref": "end-0", "change": "appear"})
            h.wait_consumed()
            seen["stale"] = dict(h.control.read_active())
            h.control.request_event("screen", "visual", qpc=time.perf_counter(),
                                    extra={"screen": "end", "ref": "end-1", "change": "appear",
                                           "score": {"mad": 1.0, "changed_pct": 0.0}, "samples": 2})
            h.wait_consumed()
            seen["appear"] = dict(h.control.read_active())

        res = self.run_session(script)
        self.assertTrue(res.ok, res.message)
        sc = self.sidecar(res)
        kinds = [(e["kind"], e.get("action"), e.get("ignored")) for e in sc["events"]]
        self.assertEqual(kinds, [("take", "open", None), ("learn", "learned", None), ("screen", "gone", None),
                                 ("screen", None, "stale-reference"), ("screen", None, "no-take-open")])
        take, learned, gone, stale, appear = sc["events"]
        self.assertEqual(seen["stale"]["feedback"]["seq"], seen["learned"]["feedback"]["seq"])   # quiet
        self.assertEqual(learned["screen_effect"], {"action": "close", "take": 1})
        self.assertEqual(sc["takes"][0]["close"]["event"], learned["id"])
        for e in (learned, gone, appear):
            self.assertIsNone(e["fiducial"])                    # no patch would ever draw over the screen
        self.assertEqual(self.flasher.calls.count(("patch", "#FF0000", "bottom-left")), 0)
        self.assertEqual(sum(1 for c in self.flasher.calls if c[0] == "patch"), 1)     # the take press only
        self.assertEqual((gone["source"], gone["score"], gone["samples"]), ("visual", {"mad": 40.0, "changed_pct": 60.0}, 2))
        auto = sc["auto_takes"]
        self.assertEqual((auto["rule"], auto["current"]), ("auto-takes/1", {"start": None, "end": "end-1"}))
        self.assertEqual(bytes.fromhex(auto["references"][0]["thumb"]), END)
        self.assertNotIn("thumb_file", auto["references"][0])
        # the PNG, for C1e, while recording; gone with the recording
        self.assertEqual(seen["png"], [f"{res.uid}.end-1.png"])
        block = seen["learned"]["auto_takes"]
        self.assertEqual((block["end"]["learned"], block["end"]["ref"], block["start"]["learned"]), (True, "end-1", False))
        png = block["end"]["thumb"]
        self.assertTrue(png.endswith(f"{res.uid}.end-1.png"))
        self.assertEqual(seen["learned"]["feedback"]["kind"], "learn")
        self.assertEqual(block["end"]["last_effect"]["action"], "close")
        self.assertEqual(seen["gone"]["feedback"]["kind"], "learn")          # a plain "gone" makes no feedback
        self.assertFalse(seen["gone"]["auto_takes"]["end"]["present"])
        self.assertEqual(seen["appear"]["feedback"]["ignored"], "no-take-open")
        self.assertEqual(seen["appear"]["auto_takes"]["end"]["seen"], 2)
        self.assertEqual(list((self.control.dir / "auto-takes").iterdir()), [])
        self.assertTrue(any("end screen learned" in line for line in self.status), self.status)

    def test_a_recording_that_learns_nothing_has_no_block_and_no_png(self):
        def script(h):
            h.control.request_event("take", "cli")
            h.wait_consumed()

        res = self.run_session(script)
        sc = self.sidecar(res)
        self.assertNotIn("auto_takes", sc)
        self.assertFalse((self.control.dir / "auto-takes").exists())

    def test_learning_while_paused_is_refused_and_recorded(self):
        def script(h):
            h.control.request_event("pause", "cli")
            h.wait_status("paused")
            h.control.request_event("learn", "hotkey", extra={"screen": "start", "refused": "paused"})
            h.wait_consumed()
            h.control.request_event("resume", "cli")
            h.wait_status("recording")

        res = self.run_session(script)
        sc = self.sidecar(res)
        learn = next(e for e in sc["events"] if e["kind"] == "learn")
        self.assertEqual((learn["accepted"], learn["ignored"]), (False, "paused"))
        self.assertNotIn("auto_takes", sc)
        json.dumps(sc)


if __name__ == "__main__":
    unittest.main()
