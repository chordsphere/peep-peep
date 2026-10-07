"""Session C1b: `Renderer.render` end to end, across a real process boundary
to tests/fake_ffmpeg.py (its analysis output synthesised from a scene, its
render writing a small file). The cut lands in the `.cut.*` namespace and
never touches an original; the sidecar's `render` block records what was
decided; failures (analysis, encode, QSV only) are loud, leave the
original untouched, and are retryable; the render lock keeps a second
render, a rename and a discard away; rename carries the cut along.

Last, a check with a real ffmpeg (skipped, with the reason, when there is none
or it lacks a feature): synthesised media, rendered with libx264,
frame-counted and colour-scanned, in a child process under a hard timeout."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import REPO, TempDirMixin, make_fake_ffmpeg
from tests import real_ffmpeg_scenario as scenario
from tests import render_fixtures as rf
from tests.test_render_plan import v1_sidecar
from peep import catalog, config as c, render

STEM = "2026-10-07-demo"
TAKES = [("take", 1, 3.0), ("mark", 1, 5.0, "intro"), ("take", 1, 12.0), ("take", 2, 2.5), ("take", 2, 10.0)]
SPEECH = {1: [(4.0, 11.0)], 2: [(3.2, 9.0)]}


class RunHarness(TempDirMixin):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "root"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        self.ffmpeg = make_fake_ffmpeg(bin_dir)
        self.argv_log = self.tmp / "argv.log"
        self.env = mock.patch.dict(os.environ, {"FAKE_FFMPEG_ARGV_LOG": str(self.argv_log),
                                                "PEEP_HOME": str(self.tmp / "home")})
        self.env.start()
        for k in ("FAKE_FFMPEG_MODE", "FAKE_FFMPEG_SCENE"):
            os.environ.pop(k, None)
        self.cfg = c.from_mapping({"root": str(self.root), "ffmpeg": {"path": str(self.ffmpeg)}})
        self.folder = self.root / "inbox"

    def tearDown(self):
        self.env.stop()
        super().tearDown()

    def record(self, sc, speech=None, **scene_kw):
        uid = rf.write_recording(self.root, sc)
        scene = rf.write_scene(self.tmp / "scene.json", rf.scene_for(sc, speech, **scene_kw))
        os.environ["FAKE_FFMPEG_SCENE"] = str(scene)
        return uid

    def sidecar(self, stem=STEM):
        return json.loads((self.folder / f"{stem}.json").read_text(encoding="utf-8"))

    def renders(self):
        if not self.argv_log.exists():
            return []
        return [a for a in map(json.loads, self.argv_log.read_text().splitlines()) if "-progress" in a]

    def listing(self):
        return sorted(os.listdir(self.folder))


class RenderRunTest(RunHarness, unittest.TestCase):
    def test_a_takes_recording_renders_its_cut(self):
        uid = self.record(rf.build_sidecar(STEM, [20.0, 15.0], TAKES), SPEECH)
        originals = {n: (self.folder / n).read_bytes() for n in self.listing() if n.endswith(".mp4")}
        steps = []
        res = render.Renderer(self.cfg).render("last", progress=lambda s, f: steps.append((s, f)))
        self.assertTrue(res.ok, res.message)
        cut = self.folder / f"{STEM}.cut.mp4"
        self.assertEqual((res.cut_path, res.uid), (cut, uid))
        self.assertTrue(cut.read_bytes().startswith(b"\0\0\0\x18ftypmp42fake-cut"))
        self.assertEqual(self.listing(), [f"{STEM}.cut.mp4", f"{STEM}.json", f"{STEM}.mp4", f"{STEM}.seg2.mp4"])
        for n, data in originals.items():
            self.assertEqual((self.folder / n).read_bytes(), data, n)          # originals untouched
        block = self.sidecar()["render"]
        self.assertEqual(block["status"], "ok")
        self.assertEqual([b["method"] for b in block["boundaries"]], ["detected"] * 4)
        self.assertEqual([b["color"] for b in block["boundaries"]], ["#0000FF", "#FF0000", "#0000FF", "#FF0000"])
        self.assertEqual([s["how"] for s in block["seams"]], ["silence", "silence", "silence", "silence"])
        self.assertEqual([ch["title"] for ch in block["chapters"]], ["start", "intro"])
        self.assertEqual([x["mark"] for x in block["concealed"]], [1])
        self.assertEqual(block["crossfades_ms"], [40.0])
        self.assertEqual(block["output"]["sha256"], render.sha256_file(cut))
        self.assertEqual(block["output"]["size_bytes"], cut.stat().st_size)
        self.assertEqual(block["file"], cut.name)
        self.assertEqual(block["fallbacks"], [])
        self.assertEqual(render.cut_status(self.sidecar(), self.folder, STEM).state, "current")
        (argv,) = self.renders()
        self.assertEqual([Path(argv[i + 1]).name for i, a in enumerate(argv) if a == "-i"],
                         [f"{STEM}.mp4", f"{STEM}.seg2.mp4", f"{STEM}.cut.part.ffmeta"])
        self.assertEqual(argv[-1], str(self.folder / f"{STEM}.cut.part.mp4"))
        self.assertEqual(steps[0], ("analysing", None))
        self.assertEqual(steps[-1], ("encoding", 1.0))
        self.assertIn("00:", res.message)

    def test_up_to_date_unless_forced(self):
        self.record(rf.build_sidecar(STEM, [20.0]))
        r = render.Renderer(self.cfg)
        self.assertTrue(r.render("last").ok)
        again = r.render("last")
        self.assertTrue(again.up_to_date)
        self.assertIn("up to date", again.message)
        self.assertEqual(len(self.renders()), 1)
        self.assertFalse(r.render("last", force=True).up_to_date)
        self.assertEqual(len(self.renders()), 2)

    def test_dry_run_decides_everything_and_writes_nothing(self):
        self.record(rf.build_sidecar(STEM, [20.0, 15.0], TAKES), SPEECH)
        side = (self.folder / f"{STEM}.json").read_bytes()
        before = self.listing()
        res = render.Renderer(self.cfg).render("last", dry_run=True)
        self.assertTrue(res.dry_run)
        self.assertEqual(self.listing(), before)
        self.assertEqual((self.folder / f"{STEM}.json").read_bytes(), side)
        self.assertEqual(self.renders(), [])
        text = "\n".join(res.report)
        self.assertIn("boundary seg1 take-open (#0000FF): detected 6 frame(s)", text)
        self.assertIn("seam seg2 end", text)
        self.assertIn("chapter 00:00 start", text)
        self.assertIn("ffmpeg: ", text)

    def test_zero_press_recording_is_trimmed_to_its_flashes(self):
        self.record(rf.build_sidecar(STEM, [20.0]), {1: [(1.5, 18.0)]})
        res = render.Renderer(self.cfg).render("last")
        block = self.sidecar()["render"]
        self.assertEqual(block["plan"], [{"segment": 1, "take": None, "start_s": 1.0, "end_s": 19.367,
                                          "frames": 551, "out_start_s": 0.0}])
        self.assertEqual(block["seams"], [])
        self.assertAlmostEqual(res.duration_s, 551 / 30, places=3)

    def test_a_v1_sidecar_renders_and_stays_v1(self):
        sc = v1_sidecar()
        sc["uid"] = catalog.new_uid()
        self.folder.mkdir(parents=True)
        (self.folder / f"{STEM}.mp4").write_bytes(b"media")
        catalog.write_sidecar(self.folder / f"{STEM}.json", sc)
        catalog.Catalog(self.root).append({"event": "recorded", "uid": sc["uid"], "collection": "inbox",
                                           "file": f"inbox/{STEM}.mp4", "title": "demo", "created": "t",
                                           "duration_s": 20.0})
        v2 = catalog.as_v2(sc)
        os.environ["FAKE_FFMPEG_SCENE"] = str(rf.write_scene(self.tmp / "scene.json", rf.scene_for(v2)))
        res = render.Renderer(self.cfg).render("last")
        self.assertTrue(res.ok)
        after = self.sidecar()
        self.assertEqual(after["schema"], catalog.SIDECAR_SCHEMA_V1)          # never converted
        self.assertEqual([b["method"] for b in after["render"]["boundaries"]], ["detected", "detected"])

    def test_a_missing_flash_falls_back_to_its_stamp_and_says_so(self):
        self.record(rf.build_sidecar(STEM, [20.0]), hide=(("segment-end", 1),))
        with self.assertLogs("peep.render", "WARNING") as logs:
            res = render.Renderer(self.cfg).render("last")
        self.assertTrue(res.ok)
        self.assertEqual(len(res.fallbacks), 1)
        self.assertIn("stop flash", res.fallbacks[0])
        self.assertIn("1 fallback(s)", res.message)
        b = self.sidecar()["render"]["boundaries"][1]
        self.assertEqual((b["method"], b["kind"]), ("stamp", "segment-end"))
        self.assertIn("render.boundary_fallback", "\n".join(logs.output))

    def test_analysis_failing_entirely_still_renders_from_the_stamps(self):
        self.record(rf.build_sidecar(STEM, [20.0, 15.0], TAKES), SPEECH)
        os.environ["FAKE_FFMPEG_MODE"] = "fail-analysis"
        res = render.Renderer(self.cfg).render("last")
        self.assertTrue(res.ok)
        block = self.sidecar()["render"]
        self.assertTrue(all(b["method"] == "stamp" for b in block["boundaries"]))
        self.assertTrue(all("ffmpeg exit 1" in b["fallback"] for b in block["boundaries"]))
        self.assertGreaterEqual(len(res.fallbacks), 4)

    def test_a_failed_encode_leaves_the_original_and_is_retryable(self):
        self.record(rf.build_sidecar(STEM, [20.0]))
        media = (self.folder / f"{STEM}.mp4").read_bytes()
        os.environ["FAKE_FFMPEG_MODE"] = "fail-render"
        with self.assertRaises(render.RenderError) as cm:
            render.Renderer(self.cfg).render("last")
        self.assertIn("ffmpeg failed (exit 1)", str(cm.exception))
        self.assertIn("device failed", str(cm.exception))
        self.assertEqual(self.listing(), [f"{STEM}.json", f"{STEM}.mp4"])      # no cut, no part, no lock
        self.assertEqual((self.folder / f"{STEM}.mp4").read_bytes(), media)
        block = self.sidecar()["render"]
        self.assertEqual(block["status"], "failed")
        self.assertIn("ffmpeg failed", block["error"])
        self.assertEqual(render.cut_status(self.sidecar(), self.folder, STEM).state, "failed")
        self.assertEqual(len(self.renders()), 2)                                # qsv, then the x264 retry
        os.environ["FAKE_FFMPEG_MODE"] = ""
        self.assertTrue(render.Renderer(self.cfg).render("last").ok)            # `peep render` retries
        self.assertEqual(self.sidecar()["render"]["status"], "ok")

    def test_a_qsv_failure_is_re_encoded_with_x264(self):
        self.record(rf.build_sidecar(STEM, [20.0]))
        os.environ["FAKE_FFMPEG_MODE"] = "fail-render-qsv"
        res = render.Renderer(self.cfg).render("last")
        self.assertTrue(res.ok)
        block = self.sidecar()["render"]
        self.assertEqual([(a["pipeline"], a["exit_code"]) for a in block["attempts"]], [("qsv", 1), ("x264", 0)])
        self.assertEqual((block["output"]["pipeline"], block["output"]["encoder"]), ("x264", "libx264"))
        self.assertIn("re-encoded with x264", res.fallbacks[-1])
        self.assertEqual([a[a.index("-c:v") + 1] for a in self.renders()], ["h264_qsv", "libx264"])

    def test_cancel_stops_without_a_retry(self):
        self.record(rf.build_sidecar(STEM, [20.0]))
        os.environ["FAKE_FFMPEG_MODE"] = "slow-render"
        r = render.Renderer(self.cfg)
        out = {}

        def go():
            try:
                r.render("last")
            except render.RenderError as exc:
                out["error"] = str(exc)
        t = threading.Thread(target=go)
        t.start()
        deadline = time.monotonic() + 20
        while (r._proc is None or not self.renders()) and time.monotonic() < deadline:
            time.sleep(0.02)                     # the encode is running (it has logged its argv)
        r.cancel()
        t.join(20)
        self.assertIn("cancelled", out.get("error", ""))
        self.assertEqual(len(self.renders()), 1)
        self.assertEqual(self.listing(), [f"{STEM}.json", f"{STEM}.mp4"])

    def test_refusals(self):
        uid = self.record(rf.build_sidecar(STEM, [20.0]))
        with self.assertRaises(catalog.RecordingInProgress):
            render.Renderer(self.cfg).render("last", live_uid=uid)
        (self.folder / f"{STEM}.mp4").unlink()
        with self.assertRaisesRegex(render.RenderError, "segment 1's file is missing"):
            render.Renderer(self.cfg).render("last")
        cfg = c.from_mapping({"root": str(self.root), "ffmpeg": {"path": str(self.tmp / "nope" / "ffmpeg")}})
        (self.folder / f"{STEM}.mp4").write_bytes(b"x")
        with self.assertRaisesRegex(render.RenderError, "ffmpeg not found"):
            render.Renderer(cfg).render("last")

    def test_a_failed_capture_and_an_all_discarded_recording_have_nothing_to_render(self):
        sc = rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 6.0)])
        sc["summary"]["kept"] = []
        self.record(sc)
        with self.assertRaisesRegex(render.RenderError, "nothing is kept"):
            render.Renderer(self.cfg).render("last")
        catalog.Catalog(self.root).append({"event": "failed", "uid": sc["uid"], "collection": "inbox",
                                           "file": f"inbox/{STEM}.mp4", "title": "x", "created": "t"})
        with self.assertRaisesRegex(render.RenderError, "failed capture"):
            render.Renderer(self.cfg).render(sc["uid"])


class EncodeSafetyTest(RunHarness, unittest.TestCase):
    """Review findings 5, 7, 8: a healthy slow encode is not a stall; a dry run with
    nothing left says so; an exception mid-encode never orphans ffmpeg."""

    def test_dry_run_with_nothing_left_says_so(self):
        self.record(rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 3.4)]))
        with self.assertRaisesRegex(render.RenderError, "nothing left to render"):
            render.Renderer(self.cfg).render("last", dry_run=True)

    def test_an_exception_mid_encode_kills_ffmpeg(self):
        self.record(rf.build_sidecar(STEM, [20.0]))
        os.environ["FAKE_FFMPEG_MODE"] = "slow-render"
        procs = []

        def popen(*a, **kw):
            procs.append(__import__("subprocess").Popen(*a, **kw))
            return procs[-1]

        def progress(stage, fraction):
            if stage == "encoding" and fraction:
                raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            render.Renderer(self.cfg, popen=popen).render("last", progress=progress)
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll())                       # not left running
        self.assertEqual(self.listing(), [f"{STEM}.json", f"{STEM}.mp4"])
        self.assertEqual(self.sidecar()["render"]["status"], "failed")

    def test_progress_lines_without_a_time_are_still_signs_of_life(self):
        class Slow:
            """An encode that reports out_time_us=N/A for longer than STALL_S (trim
            decoding up to a late first piece), then finishes."""
            pid = 4242

            def __init__(self):
                self.stdout = iter([b"out_time_us=N/A\n"] * 6 + [b"out_time_us=500000\n", b"progress=end\n"])
                self.stderr = __import__("io").BytesIO(b"")
                self.rc = None

            def poll(self):
                return self.rc

            def wait(self, timeout=None):
                self.rc = 0 if self.rc is None else self.rc
                return self.rc

            def kill(self):
                self.rc = -9

        class Stdout:
            def __init__(self, it):
                self.it = it

            def readline(self):
                time.sleep(0.15)
                return next(self.it, b"")

            def close(self):
                pass
        proc = Slow()
        proc.stdout = Stdout(proc.stdout)
        r = render.Renderer(self.cfg, popen=lambda *a, **kw: proc)
        with mock.patch.object(render, "STALL_S", 0.4):
            rc, tail = r._encode(["ffmpeg"], 1.0, lambda s, f: None)
        self.assertEqual(rc, 0, tail)


class LockAndNamespaceTest(RunHarness, unittest.TestCase):
    def test_a_live_render_lock_keeps_a_second_render_rename_and_discard_away(self):
        uid = self.record(rf.build_sidecar(STEM, [20.0]))
        lock = catalog.acquire_render_lock(self.folder, STEM, pid_alive=lambda pid: True)
        try:
            with self.assertRaisesRegex(catalog.RecordingInProgress, "already being rendered"):
                render.Renderer(self.cfg, pid_alive=lambda pid: True).render("last")
            with mock.patch("peep.catalog._pid_alive", lambda pid: True):
                with self.assertRaisesRegex(catalog.RecordingInProgress, "is being rendered"):
                    catalog.rename(self.root, uid, "other")
                with self.assertRaisesRegex(catalog.RecordingInProgress, "is being rendered"):
                    catalog.discard(self.root, uid)
        finally:
            catalog.release_render_lock(lock)
        self.assertFalse(catalog.render_lock_path(self.folder, STEM).exists())

    def test_a_stale_lock_is_taken_over_loudly(self):
        self.record(rf.build_sidecar(STEM, [20.0]))
        catalog.render_lock_path(self.folder, STEM).write_text(json.dumps({"pid": 999999, "started_at": "t"}))
        (self.folder / f"{STEM}.cut.part.mp4").write_bytes(b"half a render from a crash")
        with self.assertLogs("peep", "WARNING") as logs:
            res = render.Renderer(self.cfg, pid_alive=lambda pid: False).render("last")
        self.assertTrue(res.ok)
        text = "\n".join(logs.output)
        self.assertIn("render.stale_lock_removed", text)
        self.assertIn("render.leftover_removed", text)
        self.assertEqual(self.listing(), [f"{STEM}.cut.mp4", f"{STEM}.json", f"{STEM}.mp4"])

    def test_rename_carries_the_cut_and_it_stays_current(self):
        uid = self.record(rf.build_sidecar(STEM, [20.0, 10.0]))
        render.Renderer(self.cfg).render("last")
        entry = catalog.rename(self.root, uid, "bale pack demo", "bale")
        new_stem = "2026-10-07-bale-pack-demo"
        folder = self.root / "bale"
        self.assertEqual(sorted(os.listdir(folder)), [f"{new_stem}.cut.mp4", f"{new_stem}.json", f"{new_stem}.mp4",
                                                      f"{new_stem}.seg2.mp4"])
        sc = json.loads((folder / f"{new_stem}.json").read_text())
        self.assertEqual(sc["render"]["file"], f"{new_stem}.cut.mp4")
        self.assertEqual(render.cut_status(sc, folder, new_stem).state, "current")
        self.assertEqual(entry.file, f"bale/{new_stem}.mp4")

    def test_discard_takes_the_cut_too(self):
        uid = self.record(rf.build_sidecar(STEM, [20.0]))
        render.Renderer(self.cfg).render("last")
        catalog.discard(self.root, uid)
        self.assertEqual(self.listing(), [])

    def test_changed_events_make_the_cut_stale(self):
        self.record(rf.build_sidecar(STEM, [20.0], [("take", 1, 3.0), ("take", 1, 9.0)]), {1: [(4.0, 8.0)]})
        render.Renderer(self.cfg).render("last")
        sc = self.sidecar()
        sc["events"][0]["media_s"] = 3.2
        catalog.write_sidecar(self.folder / f"{STEM}.json", sc)
        self.assertEqual(render.cut_status(sc, self.folder, STEM).state, "stale")
        self.assertFalse(render.Renderer(self.cfg).render("last").up_to_date)


class RealFfmpegTest(TempDirMixin, unittest.TestCase):
    """Synthesised media through the real ffmpeg (tests/real_ffmpeg_scenario.py): the
    cut has exactly the planned frames, equally long audio, the chapters, and not
    one frame of any fiducial colour. Nothing is spawned at import; the probe and
    the scenario each run with stdin on /dev/null, in a session of their own (no
    terminal to be stopped by), under a hard timeout that kills the whole group.
    Skipped, with the reason, where there is no ffmpeg or it lacks a feature."""

    def test_the_cut_is_exact(self):
        ffmpeg, why = scenario.find_ffmpeg()
        if not ffmpeg:
            self.skipTest(why)
        argv = [sys.executable, "-B", "-m", "tests.real_ffmpeg_scenario", str(self.tmp), ffmpeg]
        proc = subprocess.Popen(argv, cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=scenario.SCENARIO_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            out, _ = proc.communicate()
            self.fail(f"the real-ffmpeg scenario ran past {scenario.SCENARIO_TIMEOUT_S} s and was killed:\n"
                      + out.decode("utf-8", "replace")[-2000:])
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        self.assertEqual(proc.returncode, 0, out.decode("utf-8", "replace")[-2000:])


class NoTerminalTest(unittest.TestCase):
    """The HOLD of 2026-10-07: an ffmpeg without -nostdin under `timeout` (a background
    process group) touched the tty and SIGTTOU stopped the group, the waiting Python
    with it. Every ffmpeg the render or its tests start says -nostdin."""

    def test_every_ffmpeg_argv_says_nostdin(self):
        spec = {"video": [], "audio": [], "noise": 0.001, "duration": 1.0}
        argvs = [render.frames_argv("ffmpeg", "a.mp4", 0, 1, None, (16, 10)),
                 render.pcm_argv("ffmpeg", "a.mp4", 0, 1, 1, None),
                 render.volume_argv("ffmpeg", "a.mp4", 0, 1, 2),
                 rf.synth_media_argv("ffmpeg", spec, Path("o.mp4"))]
        for a in argvs:
            self.assertIn("-nostdin", a, a)
        self.assertIn("-y", argvs[-1])


if __name__ == "__main__":
    unittest.main()
