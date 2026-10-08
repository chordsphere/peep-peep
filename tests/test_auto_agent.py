"""Session C1d: the agent's half of auto-takes. The sampler (driven step by
step, its grabber a script of synthetic thumbnails) and the real `Agent`:
learn chords and `peep learn|forget` commands, the capture and its refusals,
the recorder's confirmation through active.json, visual events out as
control-file requests, an agent restarted mid-recording reading a reference
back from its PNG, and sampling only while capturing. Also the CLI's
`peep learn|forget` against a live agent's control files, and the chords."""

import datetime as dt
import io
import json
import os
import threading
import time
import unittest
from unittest import mock

from peep import cli, config as c, events as ev, hotkeys as hk, pill, screens
from peep.control import AgentControl
from peep.events import EventModel, Press
from peep.sampler import GdiGrabber, Sampler
from tests import TempDirMixin
from tests.test_agent import AgentHarness

W, H = 16, 10
END = bytes((i * 7) % 256 for i in range(W * H))
START = bytes((i * 13 + 90) % 256 for i in range(W * H))
CONTENT = bytes((i * 29 + 40) % 256 for i in range(W * H))


class ScriptGrabber:
    """Returns `frame` (settable) every grab; `fail` raises."""

    def __init__(self, frame=CONTENT):
        self.frame, self.fail, self.grabs, self.closed = frame, None, 0, False

    size = (W, H)

    def grab(self):
        self.grabs += 1
        if self.fail:
            raise OSError(self.fail)
        return self.frame

    def close(self):
        self.closed = True


class SamplerTest(unittest.TestCase):
    def make(self, **kw):
        self.g = ScriptGrabber()
        self.emitted = []
        self.clock = [100.0]
        s = Sampler(lambda w: self.g, lambda k, r, ch: self.emitted.append((k, r, ch["change"], ch["qpc"])),
                    threaded=False, clock=lambda: self.clock[0], sleep=lambda x: None, **kw)
        return s

    def tick(self, s, frame, dt=0.25):
        self.g.frame = frame
        out = s.step()
        self.clock[0] += dt
        return out

    def test_samples_only_while_running_with_a_screen_learned(self):
        s = self.make()
        self.assertEqual(s.step(), [])
        self.assertEqual(self.g.grabs, 0)                      # nothing learned: never grabs
        s.activate("end", "end-1", END, present=False)
        s.step()
        self.assertEqual(self.g.grabs, 0)                      # not running (paused / not recording)
        s.set_running(True)
        s.step()
        self.assertEqual(self.g.grabs, 1)
        self.assertTrue(s.sampling)

    def test_appear_and_gone_events_with_the_first_samples_time(self):
        s = self.make()
        s.set_running(True)
        s.activate("end", "end-1", END, present=False)
        for f in (CONTENT, END, END, END, CONTENT, CONTENT):
            self.tick(s, f)
        self.assertEqual(self.emitted, [("end", "end-1", "appear", 100.25), ("end", "end-1", "gone", 101.0)])

    def test_learned_on_a_screen_it_is_present_already(self):
        s = self.make()
        s.set_running(True)
        s.activate("start", "start-1", START, present=True)
        for f in (START, START, CONTENT, CONTENT):
            self.tick(s, f)
        self.assertEqual(self.emitted, [("start", "start-1", "gone", 100.5)])
        s.activate("start", "start-1", END, present=True)     # same ref again: ignored, presence kept
        self.assertEqual(s.active()["start"]["thumb"], START)

    def test_capture_waits_for_stillness_and_reports_the_size(self):
        s = self.make(learn_wait_s=1.0)
        got = []
        s.capture(lambda *a: got.append(a))
        s.step()
        thumb, size, stable, waited, error = got[0]
        self.assertEqual((thumb, size, stable, error), (CONTENT, (W, H), True, None))
        self.assertAlmostEqual(waited, 0.1)

    def test_a_grab_failure_is_reported_and_retried_with_a_fresh_grabber(self):
        s = self.make()
        s.set_running(True)
        s.activate("end", "end-1", END)
        self.g.fail = "StretchBlt failed"
        self.assertEqual(s.step(), [])
        self.assertTrue(self.g.closed)
        st = s.stats()
        self.assertEqual((st["errors"], st["samples"]), (1, 0))
        self.assertIn("StretchBlt failed", st["last_error"])
        got = []
        s.capture(lambda *a: got.append(a))
        s.step()
        self.assertEqual((got[0][0], got[0][4] is not None), (None, True))     # the learn press hears it too
        self.g.fail = None
        s.step()
        self.assertEqual(s.stats()["samples"], 1)

    def test_a_reference_of_another_size_is_said_once_not_every_sample(self):
        """The screen's shape changed since it was learned (resolution, scaling): it can
        never match again; the agent toasts once, from stats()."""
        s = self.make()
        s.set_running(True)
        s.activate("end", "end-1", END[:-10])
        s.activate("start", "start-1", START, present=False)
        with self.assertLogs("peep.sampler", "WARNING") as logs:
            for _ in range(5):
                s.step()
        self.assertEqual(sum("size_mismatch" in m for m in logs.output), 1)
        st = s.stats()
        self.assertEqual((st["mismatched"], st["errors"], st["samples"]), (["end"], 0, 5))
        s.activate("end", "end-2", END)                       # learned again: matched again
        self.assertEqual(s.stats()["mismatched"], [])

    def test_the_grabber_follows_the_learned_size_not_a_reloaded_width(self):
        widths = []
        g = ScriptGrabber()
        s = Sampler(lambda w: (widths.append(w), g)[1], lambda *a: None, threaded=False, sleep=lambda x: None,
                    width=64)
        s.set_running(True)
        s.activate("end", "end-1", END, size=(W, H))
        s.configure(screens.Thresholds(), 4.0, 1.0, width=32)
        s.step()
        self.assertEqual(widths, [W])

    def test_a_start_screen_giving_way_to_the_end_screen_reports_the_end_screen_first(self):
        """So the model's guard opens no take on it (screens.CHANGE_ORDER)."""
        s = self.make()
        s.set_running(True)
        s.activate("start", "start-1", START, present=True)  # start first: its event would come first
        s.activate("end", "end-1", END, present=False)
        for f in (END, END):
            self.tick(s, f)
        self.assertEqual([(k, ch) for k, _, ch, _ in self.emitted], [("end", "appear"), ("start", "gone")])

    def test_an_emit_failure_is_logged_and_sampling_goes_on(self):
        s = Sampler(lambda w: ScriptGrabber(END), lambda *a: (_ for _ in ()).throw(LookupError("nothing is recording")),
                    threaded=False, sleep=lambda x: None)
        s.set_running(True)
        s.activate("end", "end-1", END, present=False)
        s.step()
        s.step()                                               # appear -> emit raises -> logged
        self.assertEqual(s.stats()["samples"], 2)

    def test_the_thread_idles_samples_and_stops(self):
        frames = ScriptGrabber(CONTENT)
        got = []
        s = Sampler(lambda w: frames, lambda k, r, ch: got.append(ch["change"]), hz=50.0)
        s.capture(lambda *a: got.append("captured"))
        deadline = time.monotonic() + 3
        while "captured" not in got and time.monotonic() < deadline:
            time.sleep(0.01)
        n = frames.grabs
        time.sleep(0.1)
        self.assertEqual(frames.grabs, n)                      # idle: nothing learned, no grabs
        s.activate("end", "end-1", END, present=False)
        s.set_running(True)
        frames.frame = END
        while "appear" not in got and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("appear", got)
        s.stop()
        self.assertFalse(s._thread.is_alive())
        self.assertTrue(frames.closed)

    def test_gdi_needs_windows(self):
        import sys
        if sys.platform == "win32":
            self.skipTest("on Windows GDI is there")
        with self.assertRaisesRegex(OSError, "Windows"):
            GdiGrabber()


class RecorderSide:
    """What the recorder does with the agent's requests, in miniature: the real
    event model decides them; active.json gets the take state and auto_takes."""

    def __init__(self, h):
        self.h = h
        self.m = EventModel(1.0)
        self.m.segment_started(1, time.perf_counter() - 10, None)
        self.seq = 0

    def claim(self, status="recording", uid="u1"):
        self.h.control.claim({"uid": uid, "origin": "terminal", "status": status, "final": "f",
                              "captured_s": 0.0, "recording_since": dt.datetime.now().astimezone().isoformat()})
        self.publish()

    def consume(self):
        recs = []
        for req in self.h.control.take_requests():
            r = self.m.press(Press.from_request(req), time.perf_counter())
            recs.append(r)
            self.seq += 1
            self.publish(feedback={**ev.press_feedback(r, self.seq), "at": dt.datetime.now().astimezone().isoformat()})
        return recs

    def publish(self, **extra):
        block = self.m.auto_takes_state()
        for k in ("start", "end"):
            ref = self.m.references[k] or {}
            block[k].update(thumb=ref.get("png"), size=ref.get("size"), thresholds=ref.get("thresholds"))
        self.h.control.update_active(take_state=self.m.take_state(time.perf_counter()), auto_takes=block, **extra)


class AgentAutoTest(AgentHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.g = ScriptGrabber(END)
        self.sampler = Sampler(lambda w: self.g, lambda k, r, ch: self.agent._emit_screen(k, r, ch), threaded=False,
                               sleep=lambda x: None)
        self.rec = RecorderSide(self)

    def agent_with(self, **mapping):
        cfg = c.from_mapping({"root": str(self.root), **mapping})
        return self.make_agent(cfg=cfg, sampler=self.sampler)

    def sent(self):
        out = []
        for p in sorted(self.state.glob("event-*.json")):
            out.append(json.loads(p.read_text(encoding="utf-8")))
        return out

    def learn(self, kind):
        self.agent.handle(f"learn_{kind}", None, time.perf_counter(), None)
        self.sampler.step()                                   # the capture
        self.assertTrue(self.ui.pump(lambda: bool(list(self.state.glob("event-*.json")))))

    def test_learn_capture_send_confirm_then_sample(self):
        a = self.agent_with()
        self.rec.claim()
        self.learn("end")
        [req] = self.sent()
        self.assertEqual((req["kind"], req["screen"], req["source"], req["size"]), ("learn", "end", "hotkey", [W, H]))
        self.assertEqual(bytes.fromhex(req["thumb"]), END)
        self.assertEqual(req["thresholds"], screens.Thresholds().to_dict())
        self.assertEqual(req["sampler"], {"hz": 4.0, "thumb_width": 64})
        self.assertFalse(req["same_as_current"])
        self.assertEqual(self.sampler.active(), {})           # not until the recorder confirms it
        [r] = self.rec.consume()
        self.assertEqual((r["action"], r["screen_effect"]["implicit"]), ("learned", True))
        a.tick()
        self.assertEqual(self.sampler.active()["end"]["ref"], req["ref"])
        self.assertTrue(self.sampler.active()["end"]["present"])
        self.assertTrue(self.sampler.sampling)
        self.assertTrue(self.ui.pills[-1].endswith("⇥ end screen learned · take 1 closed"), self.ui.pills[-1])
        # the screen goes away, comes back: two requests from the sampler
        for frame in (CONTENT, CONTENT, END, END):
            self.g.frame = frame
            self.sampler.step()
        changes = [(q["kind"], q["change"], q["source"]) for q in self.sent()]
        self.assertEqual(changes, [("screen", "gone", "visual"), ("screen", "appear", "visual")])
        recs = self.rec.consume()
        self.assertEqual([x["action"] or x["ignored"] for x in recs], ["gone", "no-take-open"])
        self.control.release()

    def test_reference_ids_are_unique_across_agent_restarts(self):
        self.agent_with()
        self.rec.claim()
        self.learn("end")
        [req] = self.sent()
        self.assertEqual(req["ref"], f"end-{os.getpid()}-1")       # a restarted agent has another pid
        self.control.release()

    def test_a_screen_like_the_other_kind_is_refused_before_anything_is_learned(self):
        a = self.agent_with()
        self.rec.claim()
        self.learn("end")
        self.rec.consume()
        a.tick()
        self.learn("start")                                    # still showing the end screen
        [req] = self.sent()
        self.assertEqual((req["screen"], req["refused"]), ("start", "same-as-end-screen"))
        self.assertNotIn("thumb", req)
        [r] = self.rec.consume()
        self.assertEqual(pill.feedback_text(ev.press_feedback(r, 1), {}), "⇥ not learned: that is the end screen")
        a.tick()
        self.assertNotIn("start", self.sampler.active())
        self.control.release()

    def test_the_same_appearance_learned_again_is_flagged(self):
        a = self.agent_with()
        self.rec.claim()
        self.learn("end")
        self.rec.consume()
        a.tick()
        self.learn("end")
        [req] = self.sent()
        self.assertTrue(req["same_as_current"])
        [r] = self.rec.consume()
        self.assertEqual(r["screen_effect"]["action"], "none")
        a.tick()
        self.assertEqual(self.sampler.active()["end"]["ref"], req["ref"])
        self.control.release()

    def test_paused_not_recording_and_off(self):
        a = self.agent_with()
        a.handle("learn_end", None, time.perf_counter(), None)
        self.assertIn("nothing is recording", self.ui.toasts[-1])
        self.rec.claim(status="paused")
        a.handle("learn_start", None, time.perf_counter(), None)
        [req] = self.sent()
        self.assertEqual((req["screen"], req["refused"]), ("start", "paused"))
        self.assertEqual(self.g.grabs, 0)                       # nothing captured while paused
        self.control.release()
        b = self.agent_with(auto_takes={"enabled": False})
        self.rec.claim()
        b.handle("learn_end", None, time.perf_counter(), None)
        self.assertIn("auto-takes are off", self.ui.toasts[-1])
        self.control.release()

    def test_sampling_follows_the_recording_status(self):
        a = self.agent_with()
        self.rec.claim()
        self.learn("end")
        self.rec.consume()
        a.tick()
        self.assertTrue(self.sampler.sampling)
        self.control.update_active(status="paused")
        a.tick()
        self.assertFalse(self.sampler.sampling)
        self.control.update_active(status="recording")
        a.tick()
        self.assertTrue(self.sampler.sampling)
        self.control.release()
        a.tick()
        self.assertEqual(self.sampler.active(), {})            # the recording is over: its screens with it
        self.assertFalse(self.sampler.stats()["running"])

    def test_peep_learn_and_forget_commands(self):
        a = self.agent_with()
        self.rec.claim()
        self.actl.send("learn end")
        a.tick()
        self.sampler.step()
        self.assertTrue(self.ui.pump(lambda: bool(self.sent())))
        self.assertEqual(self.sent()[0]["source"], "cli")
        self.rec.consume()
        a.tick()
        self.assertIn("end", self.sampler.active())
        self.actl.send("forget end")
        a.tick()
        self.assertNotIn("end", self.sampler.active())
        [r] = self.rec.consume()
        self.assertEqual(r["action"], "forgotten")
        self.control.release()

    def test_a_restarted_agent_reads_the_reference_back_from_its_png(self):
        png = self.tmp / "u1.end-9.png"
        png.write_bytes(screens.png_grey(W, H, END))
        self.rec.claim()
        self.control.update_active(auto_takes={"start": {"learned": False}, "end": {
            "learned": True, "ref": "end-9", "present": True, "thumb": str(png), "size": [W, H],
            "thresholds": screens.Thresholds(match_mad=6.0).to_dict()}})
        a = self.agent_with()
        a.tick()
        act = self.sampler.active()["end"]
        self.assertEqual((act["ref"], act["thumb"], act["present"]), ("end-9", END, True))
        self.assertEqual(self.sampler._refs["end"]["presence"].thr.match_mad, 6.0)
        # a reference whose PNG is gone: said once, not retried every tick
        self.control.update_active(auto_takes={"end": {"learned": True, "ref": "end-10", "thumb": str(png) + "x"}})
        a.tick()
        a.tick()
        self.assertEqual(sum("could not be read back" in t for t in self.ui.toasts), 1)
        self.control.release()

    def test_sampler_stats_reach_agent_json(self):
        a = self.agent_with()
        self.rec.claim()
        for _ in range(8):
            a.tick()
        self.assertIn("sampler", self.actl.read())
        self.assertEqual(self.actl.read()["sampler"]["samples"], 0)
        self.control.release()


class CliTest(TempDirMixin, unittest.TestCase):
    """`peep learn|forget` from a terminal: the request goes to the agent, the answer
    comes back as the feedback the recorder publishes."""

    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.env = mock.patch.dict("os.environ", {"PEEP_HOME": str(self.tmp)})
        self.env.start()
        self.addCleanup(self.env.stop)
        from peep.control import Control
        import os
        self.control = Control(self.state, lambda pid: pid == os.getpid())
        self.actl = AgentControl(self.state, lambda pid: pid == os.getpid())
        self.cfg = c.Config()
        patcher = mock.patch("peep.winapi.pid_alive", lambda pid: pid == os.getpid())
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv, agent_answers=None):
        out = []
        args = cli.build_parser().parse_args(argv)
        if agent_answers is not None:
            def answer():
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    cmd = self.actl.take_command()
                    if cmd:
                        agent_answers(cmd)
                        return
                    time.sleep(0.02)
            threading.Thread(target=answer, daemon=True).start()
        with mock.patch.object(cli, "_out", out.append), mock.patch.object(cli, "_err", lambda m: out.append("E:" + m)):
            code = cli.cmd_learn_forget(args, self.cfg, wait_s=2.0)
        return code, out

    def test_needs_a_recording_and_the_agent(self):
        code, out = self.run_cli(["learn", "end"])
        self.assertEqual((code, out), (1, ["E:nothing is recording: a learned screen belongs to one recording"]))
        self.control.claim({"uid": "u", "status": "recording"})
        code, out = self.run_cli(["learn", "end"])
        self.assertEqual(code, 1)
        self.assertIn("agent is not running", out[0])

    def test_prints_the_pills_feedback(self):
        self.control.claim({"uid": "u", "status": "recording"})
        self.actl.claim({"state": "ready"})

        def agent(cmd):
            self.assertEqual(cmd, "learn end")
            fb = {"seq": 1, "kind": "learn", "screen": "end", "action": "learned", "ignored": None,
                  "screen_effect": {"action": "close", "take": 1, "implicit": True}}
            self.control.update_active(feedback=fb)
        code, out = self.run_cli(["learn", "end"], agent)
        self.assertEqual((code, out), (0, ["⇥ end screen learned · take 1 closed"]))

        def refuse(cmd):
            self.control.update_active(feedback={"seq": 2, "kind": "learn", "screen": "start", "action": None,
                                                 "ignored": "same-as-end-screen"})
        code, out = self.run_cli(["learn", "start"], refuse)
        self.assertEqual((code, out), (1, ["⇥ not learned: that is the end screen"]))

    def test_no_answer_says_where_to_look(self):
        self.control.claim({"uid": "u", "status": "recording"})
        self.actl.claim({"state": "ready"})
        code, out = self.run_cli(["forget", "start"], lambda cmd: None)
        self.assertEqual(code, 1)
        self.assertIn("agent.log", out[-1])

    def test_the_parser(self):
        args = cli.build_parser().parse_args(["learn", "start", "--in", "3"])
        self.assertEqual((args.command, args.screen, args.delay), ("learn", "start", 3.0))
        with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
            cli.build_parser().parse_args(["learn", "middle"])


class RequestNameTest(TempDirMixin, unittest.TestCase):
    def test_two_requests_in_one_clock_tick_are_both_kept_in_order(self):
        """Windows Python 3.12's wall clock ticks every ~15.6 ms: the sampler can send
        a start screen's going and the end screen's coming within one tick."""
        from peep.control import Control
        ctl = Control(self.tmp, lambda pid: pid == os.getpid())
        ctl.claim({"uid": "u", "status": "recording"})
        with mock.patch("time.time_ns", lambda: 1_700_000_000_000_000_000):
            ctl.request_event("screen", "visual", extra={"screen": "start", "change": "gone"})
            ctl.request_event("screen", "visual", extra={"screen": "end", "change": "appear"})
            ctl.request_mark("cli", "m")
        got = ctl.take_requests()
        self.assertEqual([(r["kind"], r.get("screen")) for r in got],
                         [("screen", "start"), ("screen", "end"), ("mark", None)])
        (self.tmp / "event-00000000000000000001-7.json").write_text('{"kind": "take"}')    # the old name
        self.assertEqual([r["kind"] for r in ctl.take_requests()], ["take"])


class ChordTest(unittest.TestCase):
    def test_the_learn_chords_and_the_reserved_one(self):
        table = hk.table_from_agent_config(c.AgentConfig())
        self.assertEqual((table["learn_start"].text, table["learn_end"].text),
                         ("Ctrl+Alt+LeftBracket", "Ctrl+Alt+RightBracket"))
        with self.assertRaisesRegex(hk.HotkeyError, "reserved for the expanded pill"):
            hk.build_table({"learn_end": "Ctrl+Alt+Space"})
        with self.assertRaisesRegex(c.ConfigError, "reserved"):
            c.from_mapping({"agent": {"mark_hotkey": "Ctrl+Alt+Space"}})
        cfg = c.from_mapping({"agent": {"learn_start_hotkey": "Ctrl+Alt+Home", "learn_end_hotkey": "Ctrl+Alt+End"}})
        self.assertEqual(pill.key_labels(cfg.agent)["learn_end"], "End")

    def test_auto_takes_config(self):
        cfg = c.from_mapping({"auto_takes": {"sample_hz": 2, "match_mad": 6, "hysteresis": 3}})
        thr = c.screen_thresholds(cfg.auto_takes)
        self.assertEqual((cfg.auto_takes.sample_hz, thr.match_mad, thr.hysteresis), (2.0, 6.0, 3))
        for bad in ({"sample_hz": 50}, {"match_mad": 20}, {"thumb_width": 4}, {"learn_wait_ms": 0},
                    {"surprise": 1}):
            with self.subTest(bad=bad), self.assertRaises(c.ConfigError):
                c.from_mapping({"auto_takes": bad})
        with self.assertRaises(c.ConfigError):
            c.from_mapping({"render": {"screen_lookback_s": 0}})


if __name__ == "__main__":
    unittest.main()
