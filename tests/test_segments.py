"""Session C1a in the recorder, across the real process boundary to
fake_ffmpeg.py: hard pause / resume as segments of one session, take /
retake / mark requests and their corner patches, the sidecar's event
record, crash safety while paused, and naming at record time.

Every wait is bounded (bale validates in a sandbox where a hang must fail,
not stall)."""

import json
import os
import threading
import time
import unittest

from tests.test_recorder import FakeFlasher, RecorderHarness
from peep import catalog
from peep.flash import FlashRecord, patch_rect
from peep.recorder import RecordRequest

DEADLINE_S = 20


class PatchFlasher(FakeFlasher):
    """FakeFlasher that also draws corner patches (on a pretend 2560x1600 screen)."""

    def patch(self, color, duration_ms, corner, size_px, margin_px=0):
        now = time.monotonic()
        rect = patch_rect(corner, size_px, 2560, 1600, margin_px)
        self.calls.append(("patch", color, corner))
        return FlashRecord(color, duration_ms, catalog.now_iso(), now, now + duration_ms / 1000, shown=True,
                           shown_qpc=time.perf_counter(), style="patch", rect=rect, screen=(2560, 1600))


class SegmentHarness(RecorderHarness):
    def setUp(self):
        super().setUp()
        self.flasher = PatchFlasher()

    def wait_status(self, *statuses, timeout=DEADLINE_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if (self.control.read_active() or {}).get("status") in statuses:
                return self.control.read_active()
            time.sleep(0.01)
        raise AssertionError(f"status never became {statuses}: {self.control.read_active()}")

    def wait_consumed(self, timeout=DEADLINE_S):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not list(self.control.dir.glob("event-*.json")) and not list(self.control.dir.glob("mark-*.json")):
                time.sleep(0.05)
                return
            time.sleep(0.01)
        raise AssertionError("requests were never consumed")

    def run_session(self, script, req=None, cfg=None, reason="hotkey"):
        """Record; `script(self)` drives it once live; the recording stops when it returns."""
        rec = self.recorder(cfg=cfg)
        ev = threading.Event()
        errors = []

        def driver():
            try:
                self.wait_status("recording")
                script(self)
            except BaseException as exc:        # surfaced after the join; never leave it running
                errors.append(exc)
            finally:
                ev.reason = reason
                ev.set()

        t = threading.Thread(target=driver, daemon=True)
        t.start()
        try:
            res = rec.record(req or RecordRequest(), ev)
        finally:
            t.join(DEADLINE_S)
        if errors:
            raise errors[0]
        return res

    def sidecar(self, res):
        return catalog.read_sidecar(res.sidecar_path)

    def files(self, collection="inbox"):
        return sorted(p.name for p in (self.root / collection).iterdir())


class PauseResumeTest(SegmentHarness, unittest.TestCase):
    STEM = "2026-10-05-terminal-peep-peep"

    def test_pause_and_resume_make_two_segments_of_one_recording(self):
        seen = {}

        def script(h):
            h.control.request_event("pause", "cli")
            seen["paused"] = h.wait_status("paused")
            seen["files_while_paused"] = h.files()
            seen["catalog_while_paused"] = catalog.Catalog(h.root).entries()
            h.control.request_event("resume", "cli")
            seen["resumed"] = h.wait_status("recording")
            time.sleep(0.3)

        res = self.run_session(script)
        self.assertTrue(res.ok, res.message)
        s = self.STEM
        self.assertEqual(self.files(), [f"{s}.json", f"{s}.mp4", f"{s}.seg2.mp4"])
        self.assertEqual(res.media_path.name, f"{s}.mp4")
        self.assertEqual(res.segments, 2)
        sc = self.sidecar(res)
        self.assertEqual(sc["schema"], "peep.sidecar/2")
        self.assertEqual([(g["index"], g["file"], g["status"]) for g in sc["segments"]],
                         [(1, f"{s}.mp4", "ok"), (2, f"{s}.seg2.mp4", "ok")])
        seg1, seg2 = sc["segments"]
        # each segment is its own capture: its own ffmpeg, anchor and flashes
        self.assertEqual((seg1["stop_reason"], seg2["stop_reason"]), ("pause", "hotkey"))
        self.assertNotEqual(seg1["ffmpeg_started_qpc"], seg2["ffmpeg_started_qpc"])
        self.assertNotEqual(seg1["audio"]["epoch"]["anchor_qpc"], seg2["audio"]["epoch"]["anchor_qpc"])
        for g in (seg1, seg2):
            self.assertEqual((g["flash"]["start"]["color"], g["flash"]["stop"]["color"]), ("#FF00FF", "#00FF00"))
            self.assertIsNotNone(g["video_epoch_qpc_est"])
        self.assertLess(seg1["ffmpeg_started_qpc"], seg2["ffmpeg_started_qpc"])
        full = [c for c in self.flasher.calls if c[0] != "patch"]
        self.assertEqual(full, [("#FF00FF", 200), ("#00FF00", 200)] * 2)      # start/stop flashes per segment
        self.assertEqual(self.played, [str(self.tmp / "clap.wav")] * 2)       # a clap per segment, too
        # the top level still describes segment 1 (what a /1 reader expects of the .mp4 beside it)
        self.assertEqual(sc["flash"]["start"], seg1["flash"]["start"])
        self.assertEqual(sc["timeline"]["stop_reason"], "pause")
        self.assertAlmostEqual(sc["duration_s"], seg1["duration_s"] + seg2["duration_s"], places=3)
        [pause] = sc["pauses"]
        self.assertEqual(pause["after_segment"], 1)
        self.assertGreater(pause["seconds"], 0)
        self.assertEqual(sc["summary"]["segments"], 2)
        self.assertTrue(sc["summary"]["whole"])
        # while paused: status, pill data, files on disk, and already catalogued
        self.assertEqual(seen["paused"]["segment"], 1)
        self.assertGreater(seen["paused"]["captured_s"], 0)
        self.assertEqual(seen["files_while_paused"], [f"{s}.json", f"{s}.mp4"])
        [entry] = seen["catalog_while_paused"]
        self.assertEqual(entry.file, f"inbox/{s}.mp4")
        self.assertEqual(seen["resumed"]["segment"], 2)
        # the final catalog event replaces the in-progress one (same uid)
        [entry] = catalog.Catalog(self.root).entries()
        self.assertEqual(entry.duration_s, sc["duration_s"])
        events = catalog.Catalog(self.root).events()
        self.assertEqual([e.get("in_progress") for e in events], ["paused", None])
        self.assertEqual(events[-1]["segments"], 2)
        self.assertTrue(any(l.startswith("❚❚ paused after segment 1") for l in self.status))
        self.assertIn("2 segments", res.message)

    def test_a_crash_while_paused_leaves_the_finished_segment_catalogued(self):
        """What a crash would leave: the process is killed while paused. We stop the
        test at the same point and read the disk the way the next `peep ls` would."""
        snap = {}

        def script(h):
            h.control.request_event("pause", "cli")
            h.wait_status("paused")
            snap["entries"] = catalog.Catalog(h.root).entries()
            snap["sidecar"] = catalog.read_sidecar(h.root / "inbox" / f"{self.STEM}.json")

        self.run_session(script)
        [e] = snap["entries"]
        self.assertEqual((e.status, e.file), ("ok", f"inbox/{self.STEM}.mp4"))
        self.assertGreater(e.duration_s, 0)
        self.assertEqual(snap["sidecar"]["status"], "paused")
        self.assertEqual([g["status"] for g in snap["sidecar"]["segments"]], ["ok"])

    def test_stop_while_paused_ends_the_recording(self):
        def script(h):
            h.control.request_event("pause", "cli")
            h.wait_status("paused")
            h.control.request_stop()
            deadline = time.monotonic() + DEADLINE_S
            while h.control.read_active() is not None and time.monotonic() < deadline:
                time.sleep(0.02)

        res = self.run_session(script)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.segments, 1)
        sc = self.sidecar(res)
        self.assertEqual(sc["status"], "ok")
        self.assertEqual(sc["segments"][0]["stop_reason"], "pause")
        self.assertEqual(self.files(), [f"{self.STEM}.json", f"{self.STEM}.mp4"])

    def test_a_take_closed_while_paused_and_a_stop_while_paused(self):
        def script(h):
            h.control.request_event("take", "cli")
            h.wait_consumed()
            h.control.request_event("pause", "cli")
            h.wait_status("paused")
            h.control.request_event("take", "cli")       # closes the take, at the end of segment 1
            h.control.request_stop()                     # and the recording ends while paused
            deadline = time.monotonic() + DEADLINE_S
            while h.control.read_active() is not None and time.monotonic() < deadline:
                time.sleep(0.02)

        res = self.run_session(script)
        sc = self.sidecar(res)
        [take] = sc["takes"]
        self.assertEqual((take["close"]["after_segment"], take["close"]["segment"]), (1, None))
        self.assertEqual(take["status"], "kept")
        seg1 = sc["segments"][0]
        self.assertEqual(sc["summary"]["kept"][0]["end_s"], round(seg1["duration_s"], 3))

    def test_a_stop_that_beats_a_pause_leaves_no_pause_in_the_record(self):
        def script(h):
            h.control.request_stop()                     # written first: the recorder sees the stop first
            h.control.request_event("pause", "cli")
            deadline = time.monotonic() + DEADLINE_S
            while h.control.read_active() is not None and time.monotonic() < deadline:
                time.sleep(0.02)

        res = self.run_session(script)
        sc = self.sidecar(res)
        self.assertEqual(sc["pauses"], [])
        self.assertEqual(len(sc["segments"]), 1)
        for e in sc["events"]:                           # if the pause press was seen, it is marked superseded
            self.assertEqual((e["accepted"], e["ignored"]), (False, "superseded-by-stop"))

    def test_the_paused_recording_cannot_be_renamed_from_wsl(self):
        """The independent review's reproduction: `peep rename last x` while paused."""
        from peep import catalog as cat
        seen = {}

        def script(h):
            h.control.request_event("pause", "cli")
            info = h.wait_status("paused")
            try:
                cat.rename(h.root, "last", "renamed-now", live_uid=info["uid"])
            except cat.RecordingInProgress as exc:
                seen["refused"] = str(exc)
            h.control.request_event("resume", "cli")
            h.wait_status("recording")
            time.sleep(0.2)

        res = self.run_session(script)
        self.assertIn("still recording (or paused)", seen["refused"])
        s = self.STEM
        self.assertEqual(self.files(), [f"{s}.json", f"{s}.mp4", f"{s}.seg2.mp4"])
        self.assertEqual(catalog.Catalog(self.root).last().file, f"inbox/{s}.mp4")
        self.assertTrue(res.ok)

    def test_a_resume_that_cannot_start_keeps_what_was_captured(self):
        def script(h):
            h.control.request_event("pause", "cli")
            h.wait_status("paused")
            os.environ["FAKE_FFMPEG_MODE"] = "fail-all"     # every pipeline of segment 2 fails to start
            h.control.request_event("resume", "cli")
            deadline = time.monotonic() + DEADLINE_S
            while h.control.read_active() is not None and time.monotonic() < deadline:
                time.sleep(0.02)

        res = self.run_session(script)
        self.assertTrue(res.ok, res.message)
        self.assertEqual(res.segments, 1)
        self.assertEqual(self.files(), [f"{self.STEM}.json", f"{self.STEM}.mp4"])
        self.assertTrue(any("segment 2 could not start" in s for s in self.status), self.status)

    def test_a_later_segment_crashing_keeps_the_session_and_says_so(self):
        def script(h):
            h.control.request_event("pause", "cli")
            h.wait_status("paused")
            os.environ["FAKE_FFMPEG_MODE"] = "die-midway"   # segment 2's ffmpeg dies a second in
            h.control.request_event("resume", "cli")
            deadline = time.monotonic() + DEADLINE_S
            while h.control.read_active() is not None and time.monotonic() < deadline:
                time.sleep(0.02)

        res = self.run_session(script)
        self.assertTrue(res.ok, res.message)                  # segment 1 is good footage: not a failed recording
        s = self.STEM
        self.assertEqual(self.files(), [f"{s}.json", f"{s}.mp4", f"{s}.seg2.mkv"])
        sc = self.sidecar(res)
        self.assertEqual([(g["status"], g["file"]) for g in sc["segments"]],
                         [("ok", f"{s}.mp4"), ("failed", f"{s}.seg2.mkv")])
        self.assertEqual(sc["segment_failures"], 1)
        self.assertTrue(any(f"segment 2 ended badly (ffmpeg exit 3); what it captured is kept as {s}.seg2.mkv" in l
                            for l in self.status), self.status)

    def test_take_open_across_a_pause_and_kept_time(self):
        def script(h):
            h.control.request_event("take", "cli")
            time.sleep(0.3)
            h.control.request_event("pause", "cli")
            h.wait_status("paused")
            h.control.request_event("resume", "cli")
            h.wait_status("recording")
            time.sleep(0.3)
            h.control.request_event("take", "cli")
            h.wait_consumed()

        res = self.run_session(script)
        sc = self.sidecar(res)
        [take] = sc["takes"]
        self.assertEqual((take["open"]["segment"], take["close"]["segment"], take["status"]), (1, 2, "kept"))
        summary = sc["summary"]
        self.assertEqual([k["segment"] for k in summary["kept"]], [1, 2])
        self.assertLess(summary["kept_s"], summary["total_s"])
        self.assertEqual(res.summary, summary)


class TakeRetakeTest(SegmentHarness, unittest.TestCase):
    def test_take_retake_events_patches_and_sidecar(self):
        seen = {}

        def script(h):
            h.control.request_event("take", "hotkey")
            h.wait_consumed()
            seen["open"] = dict(h.control.read_active())
            h.control.request_event("take", "hotkey", qpc=time.perf_counter() + 0.0)   # a bounce: ignored
            h.wait_consumed()
            time.sleep(1.1)
            h.control.request_event("retake", "hotkey")
            h.wait_consumed()
            h.control.request_mark("cli", "chapter")
            h.wait_consumed()

        res = self.run_session(script)
        self.assertTrue(res.ok, res.message)
        sc = self.sidecar(res)
        kinds = [(e["kind"], e["accepted"], e["ignored"], e["action"]) for e in sc["events"]]
        self.assertEqual(kinds, [("take", True, None, "open"), ("take", False, "debounce", None),
                                 ("retake", True, None, "open"), ("mark", True, None, "mark")])
        take_ev, bounce, retake, mark = sc["events"]
        self.assertEqual(retake["discarded_take"], 1)
        for e in (take_ev, retake, mark):
            self.assertEqual(e["segment"], 1)
            self.assertIsNotNone(e["qpc"])
            self.assertEqual(e["fiducial"]["style"], "patch")
            self.assertEqual(e["fiducial"]["rect"], {"x": 0, "y": 1400, "w": 200, "h": 200})   # opposite the pill
            self.assertEqual(e["fiducial"]["screen"], {"w": 2560, "h": 1600})
        self.assertEqual(take_ev["fiducial"]["color"], "#0000FF")
        self.assertEqual(take_ev["fiducial"]["kind"], "take-open")
        self.assertEqual(retake["fiducial"]["color"], "#FFFF00")
        self.assertEqual(mark["fiducial"]["color"], "#00FFFF")
        self.assertIsNone(bounce["fiducial"])
        self.assertEqual(self.flasher.calls[1:4], [("patch", "#0000FF", "bottom-left"),
                                                   ("patch", "#FFFF00", "bottom-left"),
                                                   ("patch", "#00FFFF", "bottom-left")])
        # B's marks list keeps its shape, with the segment added
        [m] = sc["marks"]
        self.assertEqual((m["label"], m["segment"], m["flash"]["style"]), ("chapter", 1, "patch"))
        self.assertEqual([(t["id"], t["status"]) for t in sc["takes"]], [(1, "discarded"), (2, "kept")])
        self.assertEqual(sc["takes"][1]["close"]["reason"], "session-end")
        self.assertEqual(seen["open"]["takes"], 1)
        self.assertTrue(seen["open"]["take_open"])
        self.assertTrue(any("ignored (debounce)" in s for s in self.status))
        self.assertIn("1 take, ", res.message)

    def test_mark_style_full_keeps_the_full_screen_flash(self):
        cfg = self.cfg(flash={"mark_style": "full"})
        res = self.run_session(lambda h: (h.control.request_mark("cli"), h.wait_consumed()), cfg=cfg)
        m = self.sidecar(res)["marks"][0]
        self.assertNotIn("style", m["flash"])
        self.assertIn(("#00FFFF", 200), self.flasher.calls)

    def test_no_flash_means_no_patches_but_the_events_are_kept(self):
        res = self.run_session(lambda h: (h.control.request_event("take", "cli"), h.wait_consumed()),
                               req=RecordRequest(flash=False))
        sc = self.sidecar(res)
        self.assertEqual(self.flasher.calls, [])
        self.assertIsNone(sc["events"][0]["fiducial"])
        self.assertEqual(sc["events"][0]["action"], "open")

    def test_events_are_on_disk_before_the_recording_ends(self):
        seen = {}

        def script(h):
            h.control.request_event("take", "cli")
            h.wait_consumed()
            seen["sc"] = catalog.read_sidecar(h.control.read_active()["sidecar"])

        self.run_session(script)
        self.assertEqual(seen["sc"]["status"], "recording")
        self.assertEqual(seen["sc"]["events"][0]["action"], "open")
        self.assertEqual(seen["sc"]["takes"][0]["status"], "open")

    def test_patch_corner_follows_the_config(self):
        cfg = self.cfg(flash={"patch_corner": "top-right", "patch_size_px": 300, "patch_margin_px": 10})
        res = self.run_session(lambda h: (h.control.request_event("take", "cli"), h.wait_consumed()), cfg=cfg)
        self.assertEqual(self.sidecar(res)["events"][0]["fiducial"]["rect"], {"x": 2250, "y": 10, "w": 300, "h": 300})


class SingleSegmentUnchangedTest(SegmentHarness, unittest.TestCase):
    def test_zero_presses_end_exactly_as_before(self):
        res = self.record()
        self.assertTrue(res.ok)
        stem = "2026-10-05-terminal-peep-peep"
        self.assertEqual(self.files(), [f"{stem}.json", f"{stem}.mp4"])
        self.assertEqual(res.message, f"saved {res.media_path} ({res.duration_s:.1f}s)")
        ev = catalog.Catalog(self.root).events()
        self.assertEqual(len(ev), 1)
        self.assertEqual(set(ev[0]), {"ts", "event", "uid", "collection", "file", "title", "created", "duration_s"})
        sc = self.sidecar(res)
        self.assertEqual(sc["events"], [])
        self.assertEqual(sc["takes"], [])
        self.assertEqual(sc["pauses"], [])
        self.assertEqual(sc["summary"]["kept_s"], sc["summary"]["total_s"])
        self.assertTrue(sc["summary"]["whole"])
        [seg] = sc["segments"]
        self.assertEqual(seg["file"], f"{stem}.mp4")
        self.assertEqual(seg["duration_s"], sc["duration_s"])
        self.assertEqual(seg["flash"]["start"], sc["flash"]["start"])
        self.assertEqual(seg["audio"], sc["audio"])


class RecordNamingTest(SegmentHarness, unittest.TestCase):
    def test_a_case_variant_on_disk_and_a_catalogued_name_are_both_taken(self):
        coll = self.root / "inbox"
        coll.mkdir(parents=True)
        (coll / "2026-10-05-Demo.mp4").write_bytes(b"someone else's")
        catalog.Catalog(self.root).append({"event": "recorded", "uid": "a" * 32, "collection": "inbox",
                                           "file": "inbox/2026-10-05-demo-2.mp4", "title": "moved away",
                                           "created": "c", "duration_s": 1.0})
        res = self.record(RecordRequest(slug="demo"))
        self.assertEqual(res.media_path.name, "2026-10-05-demo-3.mp4")
        self.assertEqual((coll / "2026-10-05-Demo.mp4").read_bytes(), b"someone else's")
        self.assertEqual(res.suffixed_from, "2026-10-05-demo")
        self.assertEqual(self.sidecar(res)["name_suffixed_from"], "2026-10-05-demo")
        self.assertTrue(any("2026-10-05-demo was taken, so this one is 2026-10-05-demo-3" in s for s in self.status))

    def test_a_sidecar_appearing_after_allocation_is_never_overwritten(self):
        from unittest import mock
        from peep import naming
        coll = self.root / "inbox"
        coll.mkdir(parents=True)
        other = coll / "2026-10-05-demo.json"
        real = naming.allocate_stem

        def racy(*a, **kw):
            stem = real(*a, **kw)
            other.write_text('{"someone": "else"}')          # created between allocation and creation
            return stem

        with mock.patch.object(naming, "allocate_stem", side_effect=racy):
            with self.assertRaisesRegex(Exception, "nothing was overwritten"):
                self.record(RecordRequest(slug="demo"))
        self.assertEqual(json.loads(other.read_text()), {"someone": "else"})
        self.assertIsNone(self.control.read_active())


if __name__ == "__main__":
    unittest.main()
