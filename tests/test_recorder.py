"""Process control and the record flow, across a real process boundary to
fake_ffmpeg.py (no ffmpeg.exe, no display, no interop)."""

import datetime as dt
import io
import os
import threading
import time
import unittest

from tests import TempDirMixin, make_fake_ffmpeg
from peep import catalog, config as c
from peep.control import AlreadyRecording, Control
from peep.flash import FlashRecord
from peep.logsetup import parse_kv
from peep.recorder import FfmpegProcess, RecordError, Recorder, RecordRequest
from peep.winapi import ForegroundInfo

WT = r"C:\Program Files\WindowsApps\Microsoft.WindowsTerminal_1.24.11911.0_x64__8wekyb3d8bbwe\WindowsTerminal.exe"


class FakeFlasher:
    def __init__(self, fail_prepare=False):
        self.calls, self.fail_prepare, self.closed = [], fail_prepare, False

    def prepare(self):
        if self.fail_prepare:
            raise RuntimeError("no desktop")

    def flash(self, color, duration_ms):
        now = time.monotonic()
        self.calls.append((color, duration_ms))
        return FlashRecord(color, duration_ms, catalog.now_iso(), now, now + duration_ms / 1000, shown=True)

    def close(self):
        self.closed = True


class RecorderHarness(TempDirMixin):
    def setUp(self):
        super().setUp()
        self.ffmpeg = make_fake_ffmpeg(self.tmp)
        self.root = self.tmp / "Videos" / "peep"
        self.flasher = FakeFlasher()
        self.status = []
        self.control = Control(self.tmp / "state", pid_alive=lambda pid: pid == os.getpid())
        os.environ.pop("FAKE_FFMPEG_MODE", None)

    def tearDown(self):
        os.environ.pop("FAKE_FFMPEG_MODE", None)
        super().tearDown()

    def cfg(self, **sections):
        data = {"root": str(self.root), "ffmpeg": {"path": str(self.ffmpeg), "startup_timeout_s": 10,
                                                   "stop_timeout_s": 5}}
        for k, v in sections.items():
            data.setdefault(k, {}).update(v) if isinstance(v, dict) else data.__setitem__(k, v)
        return c.from_mapping(data)

    def recorder(self, cfg=None, flasher=None):
        return Recorder(cfg or self.cfg(), self.control, flasher_factory=lambda: flasher or self.flasher,
                        foreground=lambda: ForegroundInfo("chordsphere@chordsphere: ~/peep-peep", WT),
                        job_factory=lambda: None, today=lambda: dt.date(2026, 10, 5),
                        sleep=lambda s: None, status=self.status.append)

    def record(self, req=None, stop_after=0.4, **kw):
        ev = threading.Event()
        timer = threading.Timer(stop_after, ev.set)
        timer.start()
        try:
            return self.recorder(**kw).record(req or RecordRequest(), ev)
        finally:
            timer.cancel()


class RecordFlowTest(RecorderHarness, unittest.TestCase):
    def test_happy_path_end_to_end(self):
        with self.assertLogs("peep", "INFO") as logs:
            res = self.record()
        self.assertTrue(res.ok, res.message)
        media = self.root / "inbox" / "2026-10-05-terminal-peep-peep.mp4"
        self.assertEqual(res.media_path, media)
        self.assertTrue(media.exists())
        self.assertFalse((self.root / "inbox" / "2026-10-05-terminal-peep-peep.recording.mkv").exists())

        sc = catalog.read_sidecar(self.root / "inbox" / "2026-10-05-terminal-peep-peep.json")
        self.assertEqual(sc["status"], "ok")
        self.assertEqual(sc["file"], media.name)
        self.assertEqual(sc["slug"], "terminal-peep-peep")
        self.assertEqual(sc["foreground"]["process"], "WindowsTerminal.exe")
        self.assertEqual(sc["video"]["pipeline"], "qsv")
        self.assertEqual(sc["video"]["encoder"], "h264_qsv")
        self.assertEqual(sc["video"]["capture_size"], "2560x1600")
        self.assertEqual(sc["video"]["output_size"], "2560x1600")
        self.assertEqual(sc["video"]["container"], "mp4")
        self.assertEqual(sc["audio"]["device"], c.DEFAULT_MIC)
        self.assertEqual(sc["ffmpeg"]["exit_code"], 0)
        self.assertGreater(sc["duration_s"], 0)
        self.assertEqual(sc["timeline"]["stop_reason"], "terminal")
        self.assertIsNone(sc["trim"])
        # flash: start magenta, stop green, both stamped relative to the ffmpeg launch
        self.assertEqual(self.flasher.calls, [("#FF00FF", 150), ("#00FF00", 150)])
        self.assertEqual(sc["flash"]["start"]["color"], "#FF00FF")
        self.assertEqual(sc["flash"]["stop"]["color"], "#00FF00")
        self.assertLess(sc["flash"]["start"]["since_ffmpeg_start_s"], sc["flash"]["stop"]["since_ffmpeg_start_s"])
        self.assertTrue(self.flasher.closed)
        # catalog + control
        e = catalog.Catalog(self.root).last()
        self.assertEqual((e.uid, e.file, e.status), (sc["uid"], "inbox/" + media.name, "ok"))
        self.assertIsNone(self.control.read_active())
        # the capture argv was logged verbatim before ffmpeg started, and matches the sidecar
        events = [parse_kv(o.split(":", 2)[2]) for o in logs.output]
        argv_logged = [f for name, f in events if name == "ffmpeg.argv"]
        self.assertEqual(argv_logged[0]["argv"], sc["ffmpeg"]["argv"])
        self.assertEqual(argv_logged[1]["purpose"], "remux")
        names = [name for name, _ in events]
        self.assertLess(names.index("ffmpeg.argv"), names.index("ffmpeg.start"))
        self.assertIn("catalog.append", names)
        self.assertEqual(dict(events)["ffmpeg.exit"]["exit_code"], 0)

    def test_explicit_slug_collection_and_collision(self):
        req = RecordRequest(slug="Bale Pack Demo", collection="bale")
        self.assertTrue(self.record(req).ok)
        res = self.record(RecordRequest(slug="bale pack demo", collection="bale"))
        self.assertEqual(res.media_path.name, "2026-10-05-bale-pack-demo-2.mp4")
        self.assertEqual([e.file for e in catalog.Catalog(self.root).entries()],
                         ["bale/2026-10-05-bale-pack-demo.mp4", "bale/2026-10-05-bale-pack-demo-2.mp4"])

    def test_stop_via_control_file(self):
        rec = self.recorder()
        ev = threading.Event()
        threading.Timer(0.4, self.control.request_stop).start()
        res = rec.record(RecordRequest(), ev)
        self.assertTrue(res.ok, res.message)
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertEqual(sc["timeline"]["stop_reason"], "peep-stop")

    def test_mkv_container_and_no_flash_no_mic(self):
        cfg = self.cfg(output={"container": "mkv"})
        res = self.record(RecordRequest(flash=False, audio=False), cfg=cfg)
        self.assertTrue(res.ok)
        self.assertEqual(res.media_path.suffix, ".mkv")
        self.assertEqual(self.flasher.calls, [])
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertFalse(sc["flash"]["enabled"])
        self.assertIsNone(sc["audio"])
        self.assertNotIn("dshow", sc["ffmpeg"]["argv"])

    def test_fallback_when_qsv_fails_to_start(self):
        os.environ["FAKE_FFMPEG_MODE"] = "fail-qsv"
        res = self.record()
        self.assertTrue(res.ok, res.message)
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertEqual(sc["video"]["pipeline"], "x264")          # qsv and qsv-download both use h264_qsv
        self.assertEqual([a["pipeline"] for a in sc["ffmpeg"]["attempts"]], ["qsv", "qsv-download"])
        self.assertTrue(all(a["exit_code"] == 1 for a in sc["ffmpeg"]["attempts"]))
        self.assertTrue(any("qsv pipeline failed to start" in s and "trying qsv-download" in s
                            for s in self.status))
        self.assertTrue(any("MFX session" in s for s in self.status))   # ffmpeg's reason is surfaced

    def test_every_pipeline_failing(self):
        os.environ["FAKE_FFMPEG_MODE"] = "fail-all"
        res = self.record()
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 1)
        self.assertTrue(any("Conversion failed" in l for l in res.stderr_tail))
        self.assertEqual(list((self.root / "inbox").iterdir()), [])  # no clutter left behind
        self.assertEqual(self.flasher.calls, [])
        self.assertIsNone(self.control.read_active())

    def test_ffmpeg_dying_midway_keeps_the_capture(self):
        os.environ["FAKE_FFMPEG_MODE"] = "die-midway"
        res = self.record(stop_after=30)
        self.assertFalse(res.ok)
        self.assertEqual(res.exit_code, 3)
        self.assertIn("ffmpeg stopped on its own", res.message)
        self.assertEqual(res.media_path.name, "2026-10-05-terminal-peep-peep.mkv")
        self.assertTrue(res.media_path.exists())
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertEqual(sc["status"], "failed")
        self.assertTrue(any("Failed to capture" in l for l in sc["ffmpeg"]["stderr_tail"]))
        self.assertEqual(self.flasher.calls, [("#FF00FF", 150)])     # no stop flash into a dead capture
        e = catalog.Catalog(self.root).last(include_failed=True)
        self.assertEqual(e.status, "failed")
        self.assertIsNone(catalog.Catalog(self.root).last())

    def test_ffmpeg_ignoring_q_is_killed_after_timeout(self):
        os.environ["FAKE_FFMPEG_MODE"] = "ignore-q"
        cfg = self.cfg(ffmpeg={"stop_timeout_s": 1})
        res = self.record(cfg=cfg)
        self.assertFalse(res.ok)
        self.assertNotEqual(res.exit_code, 0)
        self.assertIsNone(self.control.read_active())

    def test_remux_failure_keeps_matroska(self):
        os.environ["FAKE_FFMPEG_MODE"] = "fail-remux"
        res = self.record()
        self.assertTrue(res.ok)
        self.assertEqual(res.media_path.name, "2026-10-05-terminal-peep-peep.mkv")
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertEqual(sc["remux_error"]["exit_code"], 1)
        self.assertEqual(sc["video"]["container"], "mkv")
        self.assertTrue(any("remux failed" in s for s in self.status))

    def test_flash_unavailable_records_anyway_and_says_so(self):
        res = self.record(flasher=FakeFlasher(fail_prepare=True))
        self.assertTrue(res.ok)
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertFalse(sc["flash"]["enabled"])
        self.assertIn("no desktop", sc["flash"]["error"])
        self.assertTrue(any("flash unavailable" in s for s in self.status))

    def test_scale_override_reaches_argv_and_sidecar(self):
        res = self.record(RecordRequest(scale="1920x1200"))
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertIn("vpp_qsv=w=1920:h=1200:", " ".join(sc["ffmpeg"]["argv"]))
        self.assertEqual(sc["video"]["output_size"], "1920x1200")

    def test_already_recording_is_refused_without_leaving_files(self):
        self.control.claim({"capture": "elsewhere"})
        with self.assertRaises(AlreadyRecording):
            self.record()
        self.assertEqual(list((self.root / "inbox").iterdir()), [])
        self.assertEqual(self.control.read_active()["capture"], "elsewhere")   # the live claim is untouched

    def test_timeline_starts_at_the_ffmpeg_launch(self):
        os.environ["FAKE_FFMPEG_MODE"] = "fail-qsv"          # two failed attempts before the one that runs
        res = self.record()
        sc = catalog.read_sidecar(res.sidecar_path)
        self.assertGreater(sc["timeline"]["ffmpeg_started_at"], sc["created"])
        self.assertGreaterEqual(sc["flash"]["start"]["since_ffmpeg_start_s"], sc["timeline"]["ffmpeg_ready_s"])

    def test_missing_ffmpeg(self):
        cfg = self.cfg(ffmpeg={"path": str(self.tmp / "nope" / "ffmpeg.exe")})
        with self.assertRaisesRegex(RecordError, "winget install Gyan.FFmpeg"):
            self.record(cfg=cfg)


class FfmpegProcessUnitTest(unittest.TestCase):
    class Proc:
        def __init__(self, lines, rc=None):
            self.stderr = io.BytesIO("".join(l + "\n" for l in lines).encode())
            self.stdin = io.BytesIO()
            self.pid, self._rc = 7, rc

        def poll(self):
            return self._rc

        def wait(self, timeout=None):
            return self._rc

    def start(self, lines, rc=None):
        p = FfmpegProcess(popen=lambda argv, **kw: self.Proc(lines, rc))
        p.start(["ffmpeg"])
        p.join_reader()
        return p

    def test_parses_time_and_size(self):
        p = self.start(["  Stream #0:0: Video: wrapped_avframe, d3d11, 2560x1600 [SAR 1:1 DAR 8:5], 30 fps",
                        "Output #0, matroska, to 'x.mkv':",
                        "frame=  900 fps= 30 q=-0.0 Lsize=  9000KiB time=00:01:02.50 bitrate=1kbits/s"])
        self.assertEqual(p.media_time_s(), 62.5)
        self.assertEqual(p.capture_size(), (2560, 1600))
        self.assertEqual(p.wait_ready(1), "ready")

    def test_exited_before_ready(self):
        p = self.start(["Conversion failed!"], rc=1)
        self.assertEqual(p.wait_ready(1), "exited")
        self.assertEqual(p.stderr_tail(), ["Conversion failed!"])
        self.assertIsNone(p.media_time_s())

    def test_timeout_when_silent_but_alive(self):
        p = self.start([])
        self.assertEqual(p.wait_ready(0.1), "timeout")

    def test_quit_writes_q(self):
        p = self.start(["Output #0"])
        stdin = p.proc.stdin
        stdin.close = lambda: None          # keep the BytesIO readable for the assertion
        self.assertTrue(p.send_quit())
        self.assertEqual(stdin.getvalue(), b"q")

    def test_quit_on_dead_pipe_is_reported(self):
        p = self.start(["Output #0"])
        p.proc.stdin.close()
        with self.assertLogs("peep.recorder", "WARNING") as logs:
            self.assertFalse(p.send_quit())
        self.assertIn("ffmpeg.quit_failed", logs.output[0])


if __name__ == "__main__":
    unittest.main()
