"""Session C1a's naming audit: every path that creates, renames or moves a
recording's files refuses to overwrite and suffixes on collision, checked
case-insensitively (as Windows does) against the folder and the catalog.

Also the architect's report of 2026-10-06 ("the naming convention doesn't
check if a file name exists"), reproduced from the laptop's evidence: no
bytes were overwritten, but the stop dialog prefilled the counter-less name
of an existing recording (see notes.md and `ArchitectReproductionTest`).

Sidecar versions: `peep.sidecar/1` still loads; `as_v2` gives one shape."""

import datetime as dt
import json
import unittest
from pathlib import Path
from unittest import mock

from tests import TempDirMixin
from tests.test_catalog import record
from peep import catalog as cat
from peep import dialog, naming as n

D = dt.date(2026, 10, 6)


class CaseInsensitiveFS:
    """A folder that behaves like NTFS for listing: names keep their case,
    and the allocator must treat 'Demo.mp4' and 'demo.mp4' as one."""

    def __init__(self, names):
        self.names = list(names)

    def listdir(self, directory):
        return list(self.names)


class SlugRulesTest(unittest.TestCase):
    def test_ascii_fold_kebab_and_cap(self):
        self.assertEqual(n.slug_from_user("Café Ünïcode ✓ demo!"), "cafe-unicode-demo")
        self.assertLessEqual(len(n.slug_from_user("word " * 60)), n.MAX_SLUG)
        self.assertEqual(n.slug_from_user("  trailing dots... "), "trailing-dots")

    def test_windows_reserved_names_are_refused(self):
        for name in ("CON", "con", "Nul", "aux", "PRN", "COM1", "lpt9", "COM¹"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(n.NamingError, "reserves|letters or digits"):
                    n.slug_from_user(name)
                with self.assertRaises(n.NamingError):
                    n.validate_collection(name)
        self.assertEqual(n.slug_from_user("console"), "console")       # only the exact device names
        self.assertEqual(n.validate_collection("com10"), "com10")

    def test_windows_name_problem(self):
        self.assertIsNone(n.windows_name_problem("2026-10-06-demo.mp4"))
        self.assertIn("reserves", n.windows_name_problem("nul.mp4"))   # Windows reads the part before the dot
        self.assertIn("trailing", n.windows_name_problem("demo."))
        self.assertIn("trailing", n.windows_name_problem("demo "))
        self.assertIn("character", n.windows_name_problem("a:b"))
        self.assertIn("empty", n.windows_name_problem(""))

    def test_a_window_titled_like_a_device_never_becomes_one(self):
        self.assertEqual(n.suggest_slug(None, "CON"), "recording-con")


class AllocationTest(TempDirMixin, unittest.TestCase):
    def test_case_insensitive_against_a_windows_like_folder(self):
        fs = CaseInsensitiveFS(["2026-10-06-Demo.MP4", "2026-10-06-DEMO-2.json"])
        self.assertEqual(n.allocate_stem("X", D, "demo", listdir=fs.listdir), "2026-10-06-demo-3")

    def test_case_insensitive_on_the_real_filesystem_too(self):
        (self.tmp / "2026-10-06-Demo.mp4").write_bytes(b"x")      # Linux would call this a different name
        self.assertEqual(n.allocate_stem(str(self.tmp), D, "demo"), "2026-10-06-demo-2")

    def test_the_whole_family_reserves_the_stem(self):
        for name in ("2026-10-06-a.seg2.mp4", "2026-10-06-a.cut.mp4", "2026-10-06-a.json.tmp",
                     "2026-10-06-a.seg3.recording.mkv", "2026-10-06-a.notes.txt", "2026-10-06-a"):
            with self.subTest(name=name):
                self.assertEqual(n.allocate_stem("X", D, "a", listdir=CaseInsensitiveFS([name]).listdir),
                                 "2026-10-06-a-2")

    def test_a_longer_stem_does_not_hold_a_shorter_one(self):
        fs = CaseInsensitiveFS(["2026-10-06-a-2.mp4", "2026-10-06-ab.mp4"])
        self.assertEqual(n.allocate_stem("X", D, "a", listdir=fs.listdir), "2026-10-06-a")

    def test_catalog_stems_are_taken_even_without_files(self):
        self.assertEqual(n.allocate_stem("X", D, "a", listdir=CaseInsensitiveFS([]).listdir,
                                         reserved={"2026-10-06-A"}), "2026-10-06-a-2")

    def test_ignore_lets_a_rename_keep_its_own_name(self):
        fs = CaseInsensitiveFS(["2026-10-06-a.mp4", "2026-10-06-a.json"])
        self.assertEqual(n.allocate_stem("X", D, "a", listdir=fs.listdir,
                                         ignore=["2026-10-06-A.mp4", "2026-10-06-a.json"]), "2026-10-06-a")

    def test_missing_folder_is_empty(self):
        self.assertEqual(n.allocate_stem(str(self.tmp / "nope"), D, "a"), "2026-10-06-a")

    def test_family_membership(self):
        stem = "2026-10-06-a"
        for name in ("2026-10-06-a.mp4", "2026-10-06-a.mkv", "2026-10-06-a.recording.mkv", "2026-10-06-a.json",
                     "2026-10-06-A.SEG12.MP4", "2026-10-06-a.seg2.recording.mkv", "2026-10-06-a.cut.mp4",
                     "2026-10-06-a.cut.webm", "2026-10-06-a.json.tmp"):
            self.assertTrue(n.owned_by(name, stem), name)
        for name in ("2026-10-06-a.notes.txt", "2026-10-06-a-2.mp4", "2026-10-06-ab.mp4", "2026-10-06-a"):
            self.assertFalse(n.owned_by(name, stem), name)

    def test_segment_names(self):
        self.assertEqual([n.segment_suffix(1, k) for k in ("capture", "mkv", "mp4")],
                         [".recording.mkv", ".mkv", ".mp4"])             # segment 1: A's names, unchanged
        self.assertEqual([n.segment_suffix(3, k) for k in ("capture", "mkv", "mp4")],
                         [".seg3.recording.mkv", ".seg3.mkv", ".seg3.mp4"])
        self.assertEqual(n.stem_of("2026-10-06-a.seg3.mp4"), "2026-10-06-a")
        self.assertEqual(n.slug_of_stem("2026-10-06-brave-watch-2"), "brave-watch-2")


class SafeFileOpsTest(TempDirMixin, unittest.TestCase):
    def test_exclusive_create_refuses_an_existing_sidecar(self):
        p = self.tmp / "2026-10-06-a.json"
        cat.create_json_exclusive(p, {"schema": cat.SIDECAR_SCHEMA, "uid": "1"})
        with self.assertRaises(FileExistsError):
            cat.create_json_exclusive(p, {"schema": cat.SIDECAR_SCHEMA, "uid": "2"})
        self.assertEqual(json.loads(p.read_text())["uid"], "1")

    def test_move_refuses_any_capitalisation_of_the_target(self):
        src, other = self.tmp / "a.mp4", self.tmp / "B.mp4"
        src.write_bytes(b"mine")
        other.write_bytes(b"theirs")
        with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
            cat.move_no_clobber(src, self.tmp / "b.mp4")
        self.assertEqual((src.read_bytes(), other.read_bytes()), (b"mine", b"theirs"))
        cat.move_no_clobber(src, self.tmp / "c.mp4")
        self.assertEqual((self.tmp / "c.mp4").read_bytes(), b"mine")
        self.assertFalse(src.exists())

    def test_move_without_hard_links_still_refuses(self):
        src = self.tmp / "a.mp4"
        src.write_bytes(b"mine")
        with mock.patch("os.link", side_effect=PermissionError("no links here")):
            cat.move_no_clobber(src, self.tmp / "z.mp4")
        self.assertTrue((self.tmp / "z.mp4").exists())


class RenameAuditTest(TempDirMixin, unittest.TestCase):
    def test_a_name_held_only_by_the_catalog_gets_a_counter(self):
        record(self.tmp, "inbox", "2026-10-05-demo")
        for p in (self.tmp / "inbox").glob("2026-10-05-demo.*"):
            p.unlink()                                   # moved away by hand; the catalog still lists it
        record(self.tmp, "inbox", "2026-10-05-other")
        e = cat.rename(self.tmp, "last", "demo")
        self.assertEqual(e.file, "inbox/2026-10-05-demo-2.mp4")
        self.assertTrue(e.suffixed)
        self.assertEqual(cat.Catalog(self.tmp).events()[-1]["suffixed_from"], "2026-10-05-demo")

    def test_a_case_variant_on_disk_gets_a_counter(self):
        record(self.tmp, "inbox", "2026-10-05-other")
        (self.tmp / "inbox" / "2026-10-05-Demo.mp4").write_bytes(b"someone else's")
        e = cat.rename(self.tmp, "last", "demo")
        self.assertEqual(e.file, "inbox/2026-10-05-demo-2.mp4")
        self.assertEqual((self.tmp / "inbox" / "2026-10-05-Demo.mp4").read_bytes(), b"someone else's")

    def test_every_segment_and_render_moves_with_the_recording(self):
        uid = record(self.tmp, "inbox", "2026-10-05-x")
        d = self.tmp / "inbox"
        for name in ("2026-10-05-x.seg2.mp4", "2026-10-05-x.seg3.mkv", "2026-10-05-x.cut.mp4",
                     "2026-10-05-x.notes.txt"):
            (d / name).write_bytes(name.encode())
        side = d / "2026-10-05-x.json"
        sc = cat.read_sidecar(side)
        sc["segments"] = [{"index": 1, "file": "2026-10-05-x.mp4", "capture_file": "2026-10-05-x.recording.mkv"},
                          {"index": 2, "file": "2026-10-05-x.seg2.mp4"}, {"index": 3, "file": "2026-10-05-x.seg3.mkv"}]
        cat.write_sidecar(side, sc)
        e = cat.rename(self.tmp, uid, "talk", "bale")
        b = self.tmp / "bale"
        self.assertEqual(sorted(p.name for p in b.iterdir()),
                         ["2026-10-05-talk.cut.mp4", "2026-10-05-talk.json", "2026-10-05-talk.mp4",
                          "2026-10-05-talk.seg2.mp4", "2026-10-05-talk.seg3.mkv"])
        self.assertEqual((b / "2026-10-05-talk.seg2.mp4").read_bytes(), b"2026-10-05-x.seg2.mp4")
        self.assertEqual([p.name for p in d.iterdir()], ["2026-10-05-x.notes.txt"])   # not ours: left alone
        sc = cat.read_sidecar(b / "2026-10-05-talk.json")
        self.assertEqual([s["file"] for s in sc["segments"]],
                         ["2026-10-05-talk.mp4", "2026-10-05-talk.seg2.mp4", "2026-10-05-talk.seg3.mkv"])
        self.assertEqual(sc["segments"][0]["capture_file"], "2026-10-05-talk.recording.mkv")
        self.assertEqual(e.file, "bale/2026-10-05-talk.mp4")

    def test_a_move_failing_midway_puts_everything_back(self):
        uid = record(self.tmp, "inbox", "2026-10-05-x")
        (self.tmp / "inbox" / "2026-10-05-x.seg2.mp4").write_bytes(b"s2")
        real = cat.move_no_clobber
        calls = []

        def flaky(src, dst):
            calls.append(src)
            if len(calls) == 2:
                raise PermissionError("[WinError 32] in use by a player")
            return real(src, dst)

        before = sorted(p.name for p in (self.tmp / "inbox").iterdir())
        with mock.patch.object(cat, "move_no_clobber", side_effect=flaky):
            with self.assertRaises(PermissionError):
                cat.rename(self.tmp, uid, "y")
        self.assertEqual(sorted(p.name for p in (self.tmp / "inbox").iterdir()), before)
        self.assertEqual(cat.Catalog(self.tmp).last().file, "inbox/2026-10-05-x.mp4")   # no event written

    def test_a_capture_that_never_started_cannot_be_renamed(self):
        root = self.tmp
        cat.Catalog(root).append({"event": "failed", "uid": "f" * 32, "collection": "inbox", "file": "",
                                  "title": "t", "created": "c", "duration_s": None})
        with self.assertRaises(FileNotFoundError):     # before C1a this resolved to the root folder itself
            cat.rename(root, "f" * 32, "x")
        self.assertTrue((root / "catalog.jsonl").exists())

    def test_rename_keeps_a_v1_sidecar_v1(self):
        uid = record(self.tmp, "inbox", "2026-10-05-old")
        side = self.tmp / "inbox" / "2026-10-05-old.json"
        data = json.loads(side.read_text())
        data["schema"] = cat.SIDECAR_SCHEMA_V1
        for k in ("segments", "events", "takes", "pauses", "summary"):
            data.pop(k)
        side.write_text(json.dumps(data))
        cat.rename(self.tmp, uid, "renamed")
        self.assertEqual(cat.read_sidecar(self.tmp / "inbox" / "2026-10-05-renamed.json")["schema"],
                         cat.SIDECAR_SCHEMA_V1)

    def test_plan_and_preview(self):
        uid = record(self.tmp, "movies", "2026-10-06-black-swan")
        other = record(self.tmp, "movies", "2026-10-06-brave-watch-2")
        same = cat.plan_rename(self.tmp, other, "brave watch 2")
        self.assertTrue(same.noop)
        self.assertEqual(dialog.preview_text(same), "keeps its name: movies/2026-10-06-brave-watch-2.mp4")
        taken = cat.plan_rename(self.tmp, other, "Black Swan")
        self.assertTrue(taken.suffixed)
        self.assertEqual(dialog.preview_text(taken), "saves as movies/2026-10-06-black-swan-2.mp4   "
                                                     "(2026-10-06-black-swan is taken, so a counter is added)")
        free = cat.plan_rename(self.tmp, other, "Mulholland", "films")
        self.assertEqual(dialog.preview_text(free), "saves as films/2026-10-06-mulholland.mp4")
        self.assertEqual(sorted(p.name for p in (self.tmp / "movies").iterdir())[0], "2026-10-06-black-swan.json")
        self.assertTrue(uid)


class ReviewFindingsTest(TempDirMixin, unittest.TestCase):
    """Found by an independent review of this session's change, before it shipped."""

    def test_the_live_recording_cannot_be_renamed_or_discarded(self):
        """Renaming a paused recording would have moved segment 1 and the live sidecar
        away while the recorder went on writing <oldstem>.seg2.mp4 and the old sidecar."""
        uid = record(self.tmp, "inbox", "2026-10-05-live")
        with self.assertRaisesRegex(cat.RecordingInProgress, "still recording .or paused.; rename it after it stops"):
            cat.rename(self.tmp, "last", "other", live_uid=uid)
        with self.assertRaisesRegex(cat.RecordingInProgress, "discard it after it stops"):
            cat.discard(self.tmp, uid, live_uid=uid)
        with self.assertRaises(cat.RecordingInProgress):
            cat.plan_rename(self.tmp, uid, "other", live_uid=uid)
        self.assertTrue((self.tmp / "inbox" / "2026-10-05-live.mp4").exists())
        self.assertEqual(cat.rename(self.tmp, "last", "other", live_uid="f" * 32).file,
                         "inbox/2026-10-05-other.mp4")             # another recording live: fine

    def test_a_32_character_stem_is_a_name_not_a_uid(self):
        stem = "2026-10-05-abcdefghij-klmnopqrst"
        self.assertEqual(len(stem), 32)
        record(self.tmp, "inbox", stem)
        self.assertEqual(cat.rename(self.tmp, stem, "short").file, "inbox/2026-10-05-short.mp4")
        record(self.tmp, "inbox", stem)
        self.assertEqual(cat.discard(self.tmp, stem).file, f"inbox/{stem}.mp4")   # B's discard had it too
        uid = record(self.tmp, "inbox", "2026-10-05-by-uid")
        self.assertEqual(cat.discard(self.tmp, uid).uid, uid)


class DiscardAuditTest(TempDirMixin, unittest.TestCase):
    def test_discard_takes_every_segment_and_render_and_nothing_else(self):
        keep = record(self.tmp, "inbox", "2026-10-05-x-2")
        uid = record(self.tmp, "inbox", "2026-10-05-x")
        d = self.tmp / "inbox"
        for name in ("2026-10-05-x.seg2.mp4", "2026-10-05-x.seg3.recording.mkv", "2026-10-05-x.cut.mp4",
                     "2026-10-05-x.notes.txt"):
            (d / name).write_bytes(b"x")
        cat.discard(self.tmp, uid)
        self.assertEqual(sorted(p.name for p in d.iterdir()),
                         ["2026-10-05-x-2.json", "2026-10-05-x-2.mp4", "2026-10-05-x.notes.txt"])
        ev = cat.Catalog(self.tmp).events()[-1]
        self.assertEqual(sorted(ev["removed"]), ["2026-10-05-x.cut.mp4", "2026-10-05-x.json", "2026-10-05-x.mp4",
                                                  "2026-10-05-x.seg2.mp4", "2026-10-05-x.seg3.recording.mkv"])
        self.assertEqual([e.uid for e in cat.Catalog(self.tmp).entries()], [keep])


class SidecarVersionTest(TempDirMixin, unittest.TestCase):
    V1 = {"schema": "peep.sidecar/1", "uid": "u", "title": "t", "slug": "t", "collection": "inbox",
          "file": "2026-10-04-sync.mp4", "created": "c", "status": "ok", "duration_s": 23.16,
          "foreground": {}, "video": {"pipeline": "qsv"}, "audio": {"epoch": {"video_epoch_qpc_est": 5.0}},
          "flash": {"enabled": True, "start": {"color": "#FF00FF"}, "stop": {"color": "#00FF00"}},
          "timeline": {"ffmpeg_started_at": "a", "stop_reason": "terminal", "ffmpeg_started_qpc": 4.0,
                       "input0_qpc": 5.058}, "ffmpeg": {"argv": ["ffmpeg"]}, "trim": None, "crop": None,
          "marks": [{"t": 2.0, "label": "", "flash": None}]}

    def test_v1_still_loads_and_reads_as_one_segment(self):
        p = self.tmp / "a.json"
        p.write_text(json.dumps(self.V1))
        raw = cat.read_sidecar(p)
        self.assertEqual(raw["schema"], "peep.sidecar/1")
        v2 = cat.as_v2(raw)
        self.assertEqual(raw, self.V1)                                     # the input is untouched
        self.assertEqual(v2["schema"], cat.SIDECAR_SCHEMA)
        [seg] = v2["segments"]
        self.assertEqual((seg["index"], seg["file"], seg["duration_s"], seg["video_epoch_qpc_est"]),
                         (1, "2026-10-04-sync.mp4", 23.16, 5.0))
        self.assertEqual(seg["flash"]["stop"]["color"], "#00FF00")
        self.assertEqual(seg["migrated_from"], "peep.sidecar/1")
        self.assertEqual((v2["events"], v2["takes"], v2["pauses"]), ([], [], []))
        self.assertEqual(v2["summary"], {"segments": 1, "takes": 0, "takes_discarded": 0, "kept_s": 23.16,
                                         "total_s": 23.16, "whole": True})
        self.assertEqual(v2["marks"][0]["segment"], 1)

    def test_v2_round_trips_and_as_v2_is_the_identity(self):
        sc = cat.new_sidecar(uid="u", slug="s", collection="c", file="f.mp4", created="t")
        sc["segments"] = [{"index": 1, "file": "f.mp4"}]
        sc["events"] = [{"id": 1, "kind": "take"}]
        p = self.tmp / "b.json"
        cat.write_sidecar(p, sc)
        self.assertEqual(cat.read_sidecar(p), sc)
        self.assertIs(cat.as_v2(sc), sc)

    def test_new_sidecars_are_v2_with_the_event_record(self):
        sc = cat.new_sidecar(uid="u", slug="s", collection="c", file="f.mp4", created="t")
        self.assertEqual(sc["schema"], "peep.sidecar/2")
        for key in ("segments", "events", "takes", "pauses", "summary"):
            self.assertIn(key, sc)

    def test_unknown_versions_are_still_refused(self):
        p = self.tmp / "c.json"
        p.write_text('{"schema": "peep.sidecar/3"}')
        with self.assertRaises(ValueError):
            cat.read_sidecar(p)
        with self.assertRaises(ValueError):
            cat.as_v2({"schema": "x"})

    def test_sidecar_path_for_every_file_of_a_recording(self):
        for name in ("2026-10-05-x.mp4", "2026-10-05-x.seg2.mp4", "2026-10-05-x.recording.mkv",
                     "2026-10-05-x.seg4.recording.mkv", "2026-10-05-x.cut.mp4"):
            self.assertEqual(cat.sidecar_path(Path("a") / name), Path("a/2026-10-05-x.json"))


if __name__ == "__main__":
    unittest.main()
