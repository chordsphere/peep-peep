"""Session C1b's user-facing surface. The render queue (one at a time, in
order, failures reported and survived, stop); the agent's auto-render
after the dialog (Save, Esc, no dialog, discard, render.auto, the exit
path, toasts, agent.json) and end to end with the real queue, renderer
and fake ffmpeg, including a new recording started while a render runs;
`peep render`, `peep rec`'s render after saving and --no-render, `peep
open` (the cut, --raw, the playlist of an unrendered multi-segment
recording), `peep ls`; `peep agent status`; the shim; the [render] config."""

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import make_fake_ffmpeg
from tests import render_fixtures as rf
from tests.test_agent import CHROME, AgentHarness
from tests.test_cli import CliHarness
from tests.test_shim import CFG as SHIM_CFG, DispatchTest, shim
from peep import agentcli, catalog, config as c, configedit, dialog, render
from peep.control import Control
from peep.recorder import RecordResult


class FakeResult:
    def __init__(self, ok=True, up_to_date=False, fallbacks=(), name="x.cut.mp4"):
        self.ok, self.up_to_date, self.fallbacks = ok, up_to_date, list(fallbacks)
        self.cut_path, self.duration_s, self.source_s = Path(name), 12.0, 30.0
        self.message = "cut: x" if ok else "failed"


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


class RenderQueueTest(unittest.TestCase):
    def setUp(self):
        self.events, self.lock = [], threading.Lock()

    def notify(self, kind, info):
        with self.lock:
            self.events.append((kind, info.get("uid"), info.get("fraction"), info.get("error")))

    def test_one_at_a_time_in_order(self):
        running, peak, order = [0], [0], []

        def job(uid, progress):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            order.append(uid)
            progress("encoding", 0.5)
            time.sleep(0.05)
            running[0] -= 1
            return FakeResult()
        q = render.RenderQueue(job, self.notify)
        self.assertEqual([q.submit(u, u) for u in ("a", "b", "c")][0], 0)
        self.assertTrue(q.wait_idle(10))
        self.assertEqual((order, peak[0]), (["a", "b", "c"], 1))
        kinds = [(k, u) for k, u, _, _ in self.events if k != "queued"]
        self.assertEqual(kinds, [("start", "a"), ("progress", "a"), ("done", "a"), ("start", "b"), ("progress", "b"),
                                 ("done", "b"), ("start", "c"), ("progress", "c"), ("done", "c")])
        self.assertEqual([x for x in self.events if x[0] == "progress"][0][2], 0.5)

    def test_a_failure_is_reported_and_the_queue_goes_on(self):
        def job(uid, progress):
            if uid == "bad":
                raise render.RenderError("ffmpeg failed (exit 1): boom")
            if uid == "notok":
                return FakeResult(ok=False)
            return FakeResult()
        q = render.RenderQueue(job, self.notify)
        for u in ("bad", "notok", "good"):
            q.submit(u, u)
        self.assertTrue(q.wait_idle(10))
        ends = [(k, u, e) for k, u, _, e in self.events if k in ("done", "failed")]
        self.assertEqual(ends, [("failed", "bad", "ffmpeg failed (exit 1): boom"), ("failed", "notok", "failed"),
                                ("done", "good", None)])

    def test_stop_cancels_the_running_render_and_drops_the_rest(self):
        gate, cancelled = threading.Event(), []

        def job(uid, progress):
            gate.wait(10)
            return FakeResult()
        q = render.RenderQueue(job, self.notify, cancel=lambda: (cancelled.append(True), gate.set()))
        q.submit("a", "a")
        q.submit("b", "b")
        deadline = time.monotonic() + 5
        while q.current is None and time.monotonic() < deadline:
            time.sleep(0.01)
        dropped = q.stop()
        self.assertEqual([j["uid"] for j in dropped], ["b"])
        self.assertEqual(cancelled, [True])
        with self.assertRaises(RuntimeError):
            q.submit("c", "c")
        self.assertTrue(q.wait_idle(10))

    def test_a_broken_notifier_does_not_kill_the_worker(self):
        def bad_notify(kind, info):
            raise ValueError("ui gone")
        done = []
        q = render.RenderQueue(lambda uid, progress: done.append(uid) or FakeResult(), bad_notify)
        q.submit("a", "a")
        q.submit("b", "b")
        self.assertTrue(q.wait_idle(10))
        self.assertEqual(done, ["a", "b"])


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------


class FakeQueue:
    def __init__(self):
        self.jobs, self.stopped = [], False

    def submit(self, uid, name):
        self.jobs.append((uid, name))
        return len(self.jobs) - 1

    def stop(self):
        self.stopped = True
        return []


class AgentAutoRenderTest(AgentHarness, unittest.TestCase):
    def make_q_agent(self, **cfg):
        self.q = FakeQueue()
        base = {"root": str(self.root)}
        base.update(cfg)
        return self.make_agent(c.from_mapping(base), render_queue=self.q)

    def test_save_renders_the_renamed_recording(self):
        self.make_q_agent()
        self.record_and_stop()
        self.assertEqual(self.q.jobs, [])                   # nothing before the dialog is answered
        self.assertIsNone(self.ui.dialogs[0].resolve(dialog.SAVE, "pack demo", "bale"))
        self.assertEqual(self.q.jobs, [(self.entries()[-1].uid, "2026-10-05-pack-demo.mp4")])

    def test_esc_renders_under_the_automatic_name(self):
        self.make_q_agent()
        self.record_and_stop()
        self.ui.dialogs[0].resolve(dialog.KEEP)
        self.assertEqual(self.q.jobs, [(self.entries()[-1].uid, "2026-10-05-chrome-pull-requests-peep-peep.mp4")])

    def test_discard_renders_nothing(self):
        self.make_q_agent()
        self.record_and_stop()
        self.ui.dialogs[0].resolve(dialog.DISCARD)
        self.assertEqual(self.q.jobs, [])

    def test_no_dialog_renders_straight_away(self):
        self.make_q_agent(agent={"dialog": False})
        self.record_and_stop()
        self.assertEqual(len(self.q.jobs), 1)

    def test_render_auto_never_and_takes(self):
        self.make_q_agent(render={"auto": "never"})
        self.record_and_stop()
        self.ui.dialogs[0].resolve(dialog.KEEP)
        self.assertEqual(self.q.jobs, [])
        self.make_q_agent(render={"auto": "takes"})               # the fake recorder's result has no takes
        self.record_and_stop()
        self.ui.dialogs[-1].resolve(dialog.KEEP)
        self.assertEqual(self.q.jobs, [])

    def test_a_queued_render_says_how_many_are_ahead(self):
        self.make_q_agent()
        self.q.jobs.append(("earlier", "e"))
        self.record_and_stop()
        self.ui.dialogs[0].resolve(dialog.KEEP)
        self.assertIn("render queued (1 ahead)", self.ui.toasts[-1])

    def test_exiting_agent_does_not_start_renders_and_stops_the_queue(self):
        a = self.make_q_agent()
        a.handle("record", CHROME)
        self.wait_live()
        a.shutdown("test")
        self.assertTrue(self.ui.pump(lambda: a.session is None))
        self.assertTrue(self.q.stopped)
        self.assertEqual(self.q.jobs, [])
        self.assertTrue(self.ui.quit_called)

    def test_render_events_become_toasts_and_agent_status(self):
        a = self.make_q_agent()
        a.on_render_event("start", {"uid": "u", "name": "demo.mp4"})
        self.assertEqual(self.ui.toasts[-1], "✂ rendering demo.mp4…")
        self.assertEqual(self.actl.read()["render"]["name"], "demo.mp4")
        a.on_render_event("progress", {"uid": "u", "name": "demo.mp4", "stage": "encoding", "fraction": 0.42})
        self.assertEqual(self.actl.read()["render"]["progress"], 0.42)
        lines = agentcli.status_lines(self.actl.read(), None, None, False, None)
        self.assertIn("  rendering       demo.mp4 (encoding, 42%)", lines)
        a.on_render_event("done", {"uid": "u", "name": "demo.mp4", "result": FakeResult(name="demo.cut.mp4")})
        self.assertEqual(self.ui.toasts[-1], "✂ cut ready: demo.cut.mp4 (00:12 of 00:30)")
        self.assertIsNone(self.actl.read()["render"])
        a.on_render_event("done", {"uid": "u", "name": "d", "result": FakeResult(fallbacks=["x"])})
        self.assertIn("1 fallback(s)", self.ui.toasts[-1])
        n = len(self.ui.toasts)
        a.on_render_event("done", {"uid": "u", "name": "d", "result": FakeResult(up_to_date=True)})
        self.assertEqual(len(self.ui.toasts), n)
        a.on_render_event("failed", {"uid": "u", "name": "demo.mp4", "error": "ffmpeg failed (exit 1)"})
        self.assertEqual(self.ui.toasts[-1], "✂ render failed for demo.mp4: ffmpeg failed (exit 1). The original "
                                             "is kept; `peep render` retries.")


class AgentRenderEndToEndTest(AgentHarness, unittest.TestCase):
    """The real RenderQueue and Renderer behind the agent, fake ffmpeg underneath,
    recordings made by the fixture recorder (events, flashes, patches)."""

    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.ffmpeg = make_fake_ffmpeg(bin_dir)
        self.scene = {"fps": 30, "files": {}}
        self.env = mock.patch.dict(os.environ, {"FAKE_FFMPEG_SCENE": str(self.tmp / "scene.json"),
                                                "FAKE_FFMPEG_MODE": ""})
        self.env.start()
        self.n = 0

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def make(self):
        h = self
        cfg = c.from_mapping({"root": str(self.root), "ffmpeg": {"path": str(self.ffmpeg)}})

        class FixtureRecorder:
            def __init__(self, run_cfg, status):
                self.cfg = run_cfg

            def record(self, req, ev):
                h.n += 1
                stem = f"2026-10-07-take-{h.n}"
                sc = rf.build_sidecar(stem, [20.0, 15.0], [("take", 1, 3.0), ("take", 1, 12.0), ("take", 2, 2.5),
                                                            ("take", 2, 10.0)], collection=req.collection)
                h.control.claim({"uid": sc["uid"], "origin": "agent", "status": "recording", "final": "f"})
                h.control.update_active(status="recording", recording_since=catalog.now_iso())
                try:
                    ev.wait(10)
                    rf.write_recording(h.root, sc)
                    h.scene["files"].update(rf.scene_for(sc, {1: [(4.0, 11.0)], 2: [(3.0, 9.0)]})["files"])
                    rf.write_scene(h.tmp / "scene.json", h.scene)
                finally:
                    h.control.release()
                media = h.root / req.collection / f"{stem}.mp4"
                return RecordResult(True, f"saved {media}", uid=sc["uid"], media_path=media, duration_s=35.0,
                                    segments=2, summary=sc["summary"])

        holder = {}
        queue = render.RenderQueue(lambda uid, progress: render.Renderer(holder["a"].cfg).render(uid, progress=progress),
                                   lambda kind, info: self.ui.post(holder["a"].on_render_event, kind, info))
        self.queue = queue
        from peep.agent import Agent
        from tests.test_agent import FakeListener
        a = Agent(cfg, ui=self.ui, control=self.control, agent_control=self.actl, prefs=self.prefs,
                  recorder_factory=FixtureRecorder, listener_factory=FakeListener, render_queue=queue)
        holder["a"] = a
        a.start()
        self.agent = a
        return a

    def test_keep_renders_in_the_background(self):
        a = self.make()
        self.record_and_stop()
        self.ui.dialogs[0].resolve(dialog.KEEP)
        self.assertTrue(self.ui.pump(lambda: any(t.startswith("✂ cut ready") for t in self.ui.toasts), timeout=30),
                        self.ui.toasts)
        cut = self.root / "inbox" / "2026-10-07-take-1.cut.mp4"
        self.assertTrue(cut.exists())
        sc = json.loads((self.root / "inbox" / "2026-10-07-take-1.json").read_text())
        self.assertEqual(sc["render"]["status"], "ok")
        self.assertIn("✂ rendering 2026-10-07-take-1.mp4…", self.ui.toasts)
        a.shutdown("test")

    def test_a_new_recording_starts_while_a_render_runs_and_renders_queue(self):
        os.environ["FAKE_FFMPEG_MODE"] = "slow-render"
        a = self.make()
        self.record_and_stop()
        self.ui.dialogs[0].resolve(dialog.KEEP)
        self.assertTrue(self.ui.pump(lambda: self.queue.current is not None, timeout=10))
        a.handle("record", CHROME)                          # the agent answers at once: a new recording
        self.wait_live()
        self.assertIsNotNone(self.queue.current)            # ...while the first render is still running
        a.handle("record", None)
        self.assertTrue(self.ui.pump(lambda: a.session is None, timeout=15))
        self.ui.dialogs[1].resolve(dialog.KEEP)
        self.assertTrue(self.ui.pump(lambda: sum(t.startswith("✂ cut ready") for t in self.ui.toasts) == 2,
                                     timeout=40), self.ui.toasts)
        ready = [t for t in self.ui.toasts if t.startswith("✂ cut ready")]
        self.assertIn("take-1.cut.mp4", ready[0])
        self.assertIn("take-2.cut.mp4", ready[1])
        a.shutdown("test")

    def test_a_failed_render_is_a_toast_and_the_original_stays(self):
        os.environ["FAKE_FFMPEG_MODE"] = "fail-render"
        a = self.make()
        self.record_and_stop()
        media = self.root / "inbox" / "2026-10-07-take-1.mp4"
        before = media.read_bytes()
        self.ui.dialogs[0].resolve(dialog.KEEP)
        self.assertTrue(self.ui.pump(lambda: any("render failed" in t for t in self.ui.toasts), timeout=30))
        self.assertEqual(media.read_bytes(), before)
        self.assertFalse((self.root / "inbox" / "2026-10-07-take-1.cut.mp4").exists())
        a.shutdown("test")


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


class RenderCliTest(CliHarness, unittest.TestCase):
    def setUp(self):
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.ffmpeg = make_fake_ffmpeg(bin_dir)
        home = self.tmp / "home"
        home.mkdir()
        (home / "config.toml").write_text(f'[ffmpeg]\npath = "{self.ffmpeg}"\n', encoding="utf-8")
        os.environ.pop("FAKE_FFMPEG_MODE", None)

    def record(self, durations=(20.0, 15.0), presses=(("take", 1, 3.0), ("take", 1, 12.0), ("take", 2, 2.5),
                                                       ("take", 2, 10.0)), stem="2026-10-07-demo"):
        sc = rf.build_sidecar(stem, list(durations), list(presses))
        rf.write_recording(self.root, sc)
        os.environ["FAKE_FFMPEG_SCENE"] = str(rf.write_scene(self.tmp / "scene.json",
                                                             rf.scene_for(sc, {1: [(4, 11)], 2: [(3, 9)]})))
        return sc

    def tearDown(self):
        for k in ("FAKE_FFMPEG_SCENE", "FAKE_FFMPEG_MODE"):
            os.environ.pop(k, None)
        super().tearDown()

    def test_render_prints_progress_and_the_cut(self):
        self.record()
        code, out, err = self.run_cli("render")
        self.assertEqual(code, 0, err)
        self.assertIn("finding the flashes and patches", out)
        self.assertIn("100%", out)
        self.assertIn("✓ cut: ", out)
        self.assertIn("2026-10-07-demo.cut.mp4", out)
        code, out, _ = self.run_cli("render", "last")
        self.assertIn("up to date", out)
        code, out, _ = self.run_cli("render", "--force")
        self.assertIn("✓ cut", out)

    def test_dry_run_prints_the_plan(self):
        self.record()
        code, out, _ = self.run_cli("render", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("plan: 2 segment(s)", out)
        self.assertIn("dry run: nothing was written", out)
        self.assertFalse((self.root / "inbox" / "2026-10-07-demo.cut.mp4").exists())

    def test_a_failed_render_says_why_and_exits_1(self):
        self.record()
        os.environ["FAKE_FFMPEG_MODE"] = "fail-render"
        code, _, err = self.run_cli("render")
        self.assertEqual(code, 1)
        self.assertIn("ffmpeg failed (exit 1)", err)
        self.assertIn("The original is untouched", err)

    def test_render_refuses_the_live_recording(self):
        sc = self.record()
        ctl = Control(self.tmp / "home" / "state", lambda pid: pid == os.getpid())
        ctl.claim({"uid": sc["uid"], "status": "paused", "final": "f"})
        try:
            code, _, err = self.run_cli("render")
        finally:
            ctl.release()
        self.assertEqual(code, 1)
        self.assertIn("still recording (or paused)", err)

    def fake_recorder(self, sc):
        class FakeRecorder:
            def __init__(self, cfg, control, status):
                pass

            def record(self, req, stop_event):
                return RecordResult(True, "saved X (35.0s)", uid=sc["uid"], segments=len(sc["segments"]),
                                    summary=sc["summary"])
        return mock.patch("peep.recorder.Recorder", FakeRecorder)

    def test_rec_renders_after_saving(self):
        sc = self.record()
        with self.fake_recorder(sc):
            code, out, err = self.run_cli("rec", "--stdin-stop", "off")
        self.assertEqual(code, 0, err)
        self.assertIn("✓ saved X", out)
        self.assertIn("✂ rendering the cut (render.auto = always", out)
        self.assertIn("✓ cut: ", out)

    def test_rec_no_render_and_auto_takes(self):
        sc = self.record(durations=(20.0,), presses=())
        with self.fake_recorder(sc):
            code, out, _ = self.run_cli("rec", "--stdin-stop", "off", "--no-render")
        self.assertNotIn("rendering", out)
        (self.tmp / "home" / "config.toml").write_text(f'[ffmpeg]\npath = "{self.ffmpeg}"\n[render]\nauto = "takes"\n')
        with self.fake_recorder(sc):
            code, out, _ = self.run_cli("rec", "--stdin-stop", "off")
        self.assertNotIn("rendering", out)                  # no takes, one segment: nothing to cut
        self.assertFalse((self.root / "inbox" / "2026-10-07-demo.cut.mp4").exists())

    def test_rec_render_failure_keeps_exit_0_and_says_so(self):
        sc = self.record()
        os.environ["FAKE_FFMPEG_MODE"] = "fail-render"
        with self.fake_recorder(sc):
            code, out, err = self.run_cli("rec", "--stdin-stop", "off")
        self.assertEqual(code, 0)
        self.assertIn("render failed", err)
        self.assertIn("`peep render` retries", err)

    def test_open_prefers_a_current_cut(self):
        self.record()
        with mock.patch("peep.winapi.open_with_default") as opener:
            code, out, _ = self.run_cli("open")
        playlist = self.tmp / "home" / "state" / "playlists" / "2026-10-07-demo.m3u"
        opener.assert_called_once_with(str(playlist))          # unrendered, two segments: a playlist
        self.assertEqual(playlist.read_text().splitlines()[1:],
                         [str(self.root / "inbox" / "2026-10-07-demo.mp4"),
                          str(self.root / "inbox" / "2026-10-07-demo.seg2.mp4")])
        self.assertIn("2 segments in order; not rendered yet", out)
        self.run_cli("render")
        with mock.patch("peep.winapi.open_with_default") as opener:
            code, out, _ = self.run_cli("open", "last")
        opener.assert_called_once_with(str(self.root / "inbox" / "2026-10-07-demo.cut.mp4"))
        self.assertIn("(the cut; `peep open --raw` for the original)", out)
        with mock.patch("peep.winapi.open_with_default") as opener:
            self.run_cli("open", "--raw")
        opener.assert_called_once_with(str(self.root / "inbox" / "2026-10-07-demo.mp4"))

    def test_open_says_when_the_cut_is_stale(self):
        self.record(durations=(20.0,), presses=(("take", 1, 3.0), ("take", 1, 12.0)))
        self.run_cli("render")
        side = self.root / "inbox" / "2026-10-07-demo.json"
        sc = json.loads(side.read_text())
        sc["events"][0]["media_s"] = 3.3
        catalog.write_sidecar(side, sc)
        with mock.patch("peep.winapi.open_with_default") as opener:
            code, out, _ = self.run_cli("open")
        opener.assert_called_once_with(str(self.root / "inbox" / "2026-10-07-demo.mp4"))
        self.assertIn("older than this recording's events", out)

    def test_ls_shows_segments_takes_and_the_cut(self):
        self.record()
        code, out, _ = self.run_cli("ls")
        self.assertIn("inbox/2026-10-07-demo.mp4   [2 seg · 2 takes]", out)
        self.run_cli("render")
        code, out, _ = self.run_cli("ls")
        self.assertIn("[2 seg · 2 takes · cut]", out)
        code, out, _ = self.run_cli("ls", "--json")
        row = json.loads(out)[0]
        self.assertEqual((row["segments"], row["takes"], row["cut_state"], row["cut"]),
                         (2, 2, "current", "inbox/2026-10-07-demo.cut.mp4"))


# ---------------------------------------------------------------------------
# Shim + config
# ---------------------------------------------------------------------------


class ShimRenderTest(DispatchTest):
    def test_render_passes_through(self):
        cfg = dict(SHIM_CFG, app_wsl=str(self.tmp))
        for argv in (["render"], ["render", "last", "--force"], ["render", "--dry-run"], ["open", "--raw"]):
            with self.subTest(argv=argv), \
                    mock.patch.object(shim, "load_shim_config", return_value=cfg), \
                    mock.patch.object(shim, "autosync"), \
                    mock.patch.object(shim.subprocess, "call", return_value=0) as call:
                self.assertEqual(self.run_main(*argv)[0], 0)
                self.assertEqual(call.call_args.args[0][4:], argv)
        self.assertIn("render [last|STEM] [--force] [--dry-run]", shim.USAGE)
        self.assertIn("--no-render", shim.USAGE)
        self.assertIn("--raw", shim.USAGE)


class RenderConfigTest(unittest.TestCase):
    def test_defaults_and_validation(self):
        r = c.Config().render
        self.assertEqual((r.auto, r.search_s, r.snap_ms, r.crossfade_ms, r.av_calibration),
                         ("always", 1.0, 300, 40, "measure"))
        for data, why in (({"render": {"auto": "sometimes"}}, "render.auto"),
                          ({"render": {"snap_ms": -1}}, "render.snap_ms"),
                          ({"render": {"silence_db": 0.0}}, "render.silence_db"),
                          ({"render": {"av_calibration": "fix"}}, "render.av_calibration"),
                          ({"render": {"min_interval_ms": 5}}, "render.min_interval_ms")):
            with self.subTest(data=data), self.assertRaisesRegex(c.ConfigError, why):
                c.from_mapping(data)
        self.assertEqual(c.from_mapping({"render": {"silence_db": -50}}).render.silence_db, -50.0)

    def test_template_documents_every_render_key(self):
        for key in ("auto", "search_s", "snap_ms", "crossfade_ms", "silence_db", "silence_max_trim_ms",
                    "silence_keep_ms", "min_interval_ms", "av_calibration"):
            self.assertIn(f"# {key} = ", c.TEMPLATE.split("[render]")[1])

    def test_config_set_render_auto(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            res = configedit.set_value(p, "render.auto", "takes", environ={"USERPROFILE": d})
            self.assertEqual((res.key, res.new), ("render.auto", "takes"))
            self.assertIn('auto = "takes"', p.read_text())
            with self.assertRaises(c.ConfigError):
                configedit.set_value(p, "render.auto", "maybe", environ={"USERPROFILE": d})


if __name__ == "__main__":
    unittest.main()
