"""Sidecar and catalog round-trips, the fold, resolve, and rename."""

import json
import unittest
from pathlib import Path

from tests import TempDirMixin
from peep import catalog as cat


def record(root: Path, collection: str, stem: str, title: str = None, ext: str = ".mp4",
           event: str = "recorded") -> str:
    """Create a media file + sidecar + catalog event, as the recorder would."""
    d = root / collection
    d.mkdir(parents=True, exist_ok=True)
    media = d / (stem + ext)
    media.write_bytes(b"video")
    uid = cat.new_uid()
    sc = cat.new_sidecar(uid=uid, slug=title or stem[11:], collection=collection, file=media.name,
                         created="2026-10-05T10:00:00.000-04:00")
    sc["status"] = "ok" if event == "recorded" else "failed"
    cat.write_sidecar(cat.sidecar_path(media), sc)
    cat.Catalog(root).append({"event": event, "uid": uid, "collection": collection,
                              "file": f"{collection}/{media.name}", "title": sc["title"],
                              "created": sc["created"], "duration_s": 12.5})
    return uid


class SidecarTest(TempDirMixin, unittest.TestCase):
    def test_new_sidecar_has_every_key_including_reserved(self):
        sc = cat.new_sidecar(uid="u", slug="s", collection="c", file="f.mp4", created="t")
        for key in ("schema", "uid", "title", "slug", "collection", "file", "created", "status",
                    "duration_s", "foreground", "video", "audio", "flash", "timeline", "ffmpeg",
                    "trim", "crop", "marks"):
            self.assertIn(key, sc)
        self.assertIsNone(sc["trim"])
        self.assertIsNone(sc["crop"])
        self.assertEqual(sc["marks"], [])
        self.assertEqual(sc["schema"], cat.SIDECAR_SCHEMA)

    def test_round_trip_and_atomic_write(self):
        p = self.tmp / "2026-10-05-x.json"
        sc = cat.new_sidecar(uid="u1", slug="x", collection="inbox", file="2026-10-05-x.mp4", created="t")
        sc["flash"] = {"enabled": True, "start": {"color": "#FF00FF", "duration_ms": 150}, "stop": None}
        sc["foreground"]["title"] = "Ünïcode ✓"
        cat.write_sidecar(p, sc)
        self.assertEqual(cat.read_sidecar(p), sc)
        self.assertFalse((self.tmp / "2026-10-05-x.json.tmp").exists())
        self.assertTrue(p.read_text(encoding="utf-8").endswith("}\n"))

    def test_read_rejects_foreign_json(self):
        p = self.tmp / "other.json"
        p.write_text('{"schema": "something/9"}')
        with self.assertRaises(ValueError):
            cat.read_sidecar(p)

    def test_sidecar_path(self):
        self.assertEqual(cat.sidecar_path(Path("a/2026-10-05-x.mp4")), Path("a/2026-10-05-x.json"))
        self.assertEqual(cat.sidecar_path(Path("a/2026-10-05-x.recording.mkv")), Path("a/2026-10-05-x.json"))


class CatalogTest(TempDirMixin, unittest.TestCase):
    def test_append_is_one_json_line_with_timestamp(self):
        c = cat.Catalog(self.tmp)
        c.append({"event": "recorded", "uid": "a", "file": "inbox/x.mp4"})
        c.append({"event": "renamed", "uid": "a", "from": "inbox/x.mp4", "to": "inbox/y.mp4"})
        lines = (self.tmp / cat.CATALOG_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["event"], "recorded")
        self.assertIn("ts", first)

    def test_fold_last_and_failed(self):
        a = record(self.tmp, "inbox", "2026-10-05-first")
        b = record(self.tmp, "bale", "2026-10-05-second")
        f = record(self.tmp, "inbox", "2026-10-05-broken", ext=".mkv", event="failed")
        c = cat.Catalog(self.tmp)
        entries = c.entries()
        self.assertEqual([e.uid for e in entries], [a, b, f])
        self.assertEqual(entries[2].status, "failed")
        self.assertEqual(c.last().uid, b)                      # failed captures are not "last"
        self.assertEqual(c.last(include_failed=True).uid, f)
        self.assertEqual(entries[1].media_path(self.tmp), self.tmp / "bale" / "2026-10-05-second.mp4")

    def test_empty_catalog(self):
        c = cat.Catalog(self.tmp)
        self.assertEqual(c.entries(), [])
        self.assertIsNone(c.last())
        with self.assertRaises(LookupError):
            c.resolve("last")

    def test_torn_line_is_skipped_and_logged(self):
        record(self.tmp, "inbox", "2026-10-05-ok")
        with (self.tmp / cat.CATALOG_NAME).open("a", encoding="utf-8") as fh:
            fh.write('{"event": "recorded", "uid": "tor')
        with self.assertLogs("peep.catalog", "WARNING") as logs:
            entries = cat.Catalog(self.tmp).entries()
        self.assertEqual(len(entries), 1)
        self.assertIn("catalog.bad_line", logs.output[0])

    def test_resolve(self):
        a = record(self.tmp, "inbox", "2026-10-05-alpha")
        record(self.tmp, "inbox", "2026-10-05-beta")
        c = cat.Catalog(self.tmp)
        self.assertEqual(c.resolve("2026-10-05-alpha").uid, a)
        self.assertEqual(c.resolve("inbox/2026-10-05-alpha.mp4").uid, a)
        self.assertEqual(c.resolve("2026-10-05-alpha.mp4").uid, a)
        self.assertEqual(c.resolve(a[:8]).uid, a)
        with self.assertRaises(LookupError):
            c.resolve("nope")


class RenameTest(TempDirMixin, unittest.TestCase):
    def test_rename_last_keeps_date_and_moves_sidecar(self):
        uid = record(self.tmp, "inbox", "2026-10-05-terminal-peep-peep")
        e = cat.rename(self.tmp, "last", "Bale Tutorial: Pack")
        new = self.tmp / "inbox" / "2026-10-05-bale-tutorial-pack.mp4"
        self.assertEqual(e.media_path(self.tmp), new)
        self.assertTrue(new.exists())
        self.assertFalse((self.tmp / "inbox" / "2026-10-05-terminal-peep-peep.mp4").exists())
        self.assertFalse((self.tmp / "inbox" / "2026-10-05-terminal-peep-peep.json").exists())
        sc = cat.read_sidecar(self.tmp / "inbox" / "2026-10-05-bale-tutorial-pack.json")
        self.assertEqual((sc["uid"], sc["slug"], sc["title"], sc["file"]),
                         (uid, "bale-tutorial-pack", "Bale Tutorial: Pack", new.name))
        # the catalog fold now reports the new name; last still resolves
        c = cat.Catalog(self.tmp)
        self.assertEqual(c.last().file, "inbox/2026-10-05-bale-tutorial-pack.mp4")
        self.assertEqual(c.last().title, "Bale Tutorial: Pack")
        self.assertEqual(c.events()[-1]["event"], "renamed")

    def test_rename_collision_gets_counter(self):
        record(self.tmp, "inbox", "2026-10-05-demo")
        record(self.tmp, "inbox", "2026-10-05-other")
        e = cat.rename(self.tmp, "last", "demo")
        self.assertEqual(e.file, "inbox/2026-10-05-demo-2.mp4")

    def test_rename_to_same_name_is_a_no_op(self):
        record(self.tmp, "inbox", "2026-10-05-demo")
        e = cat.rename(self.tmp, "last", "Demo")
        self.assertEqual(e.file, "inbox/2026-10-05-demo.mp4")
        self.assertTrue((self.tmp / "inbox" / "2026-10-05-demo.mp4").exists())
        self.assertEqual(len(cat.Catalog(self.tmp).events()), 1)

    def test_rename_into_collection(self):
        record(self.tmp, "inbox", "2026-10-05-demo")
        e = cat.rename(self.tmp, "last", "pack walkthrough", collection="Bale")
        self.assertEqual(e.file, "bale/2026-10-05-pack-walkthrough.mp4")
        self.assertTrue((self.tmp / "bale" / "2026-10-05-pack-walkthrough.json").exists())
        self.assertEqual(cat.Catalog(self.tmp).last().collection, "bale")

    def test_rename_missing_file_is_loud(self):
        record(self.tmp, "inbox", "2026-10-05-demo")
        (self.tmp / "inbox" / "2026-10-05-demo.mp4").unlink()
        with self.assertRaises(FileNotFoundError):
            cat.rename(self.tmp, "last", "x")

    def test_rename_bad_name(self):
        record(self.tmp, "inbox", "2026-10-05-demo")
        with self.assertRaises(ValueError):
            cat.rename(self.tmp, "last", "???")



class DiscardTest(TempDirMixin, unittest.TestCase):
    """Session B: the discard hotkey / dialog Delete."""

    def test_discard_removes_files_and_the_fold_forgets_it(self):
        keep = record(self.tmp, "inbox", "2026-10-05-keep")
        uid = record(self.tmp, "inbox", "2026-10-05-take")
        (self.tmp / "inbox" / "2026-10-05-take.recording.mkv").write_bytes(b"leftover")
        e = cat.discard(self.tmp, uid, reason="hotkey")
        self.assertEqual(e.file, "inbox/2026-10-05-take.mp4")
        self.assertEqual(sorted(p.name for p in (self.tmp / "inbox").iterdir()),
                         ["2026-10-05-keep.json", "2026-10-05-keep.mp4"])
        self.assertEqual([x.uid for x in cat.Catalog(self.tmp).entries()], [keep])
        ev = cat.Catalog(self.tmp).events()[-1]
        self.assertEqual((ev["event"], ev["uid"], ev["reason"]), ("discarded", uid, "hotkey"))
        self.assertEqual(sorted(ev["removed"]), ["2026-10-05-take.json", "2026-10-05-take.mp4",
                                                  "2026-10-05-take.recording.mkv"])
        with self.assertRaises(LookupError):
            cat.discard(self.tmp, uid)                       # gone from the fold

    def test_discard_by_last_and_failed_takes(self):
        record(self.tmp, "inbox", "2026-10-05-ok")
        failed = record(self.tmp, "inbox", "2026-10-05-broken", ext=".mkv", event="failed")
        cat.discard(self.tmp, failed)
        self.assertFalse((self.tmp / "inbox" / "2026-10-05-broken.mkv").exists())
        cat.discard(self.tmp, "last")
        self.assertEqual(cat.Catalog(self.tmp).entries(), [])

    def test_already_missing_files_are_logged_not_fatal(self):
        uid = record(self.tmp, "inbox", "2026-10-05-gone")
        for p in (self.tmp / "inbox").iterdir():
            p.unlink()
        with self.assertLogs("peep.catalog", "WARNING") as logs:
            cat.discard(self.tmp, uid)
        self.assertIn("discard.nothing_on_disk", logs.output[0])
        self.assertEqual(cat.Catalog(self.tmp).entries(), [])

    def test_undeletable_file_raises_before_the_event(self):
        uid = record(self.tmp, "inbox", "2026-10-05-locked")
        from unittest import mock
        with mock.patch("os.remove", side_effect=PermissionError("[WinError 32] in use")):
            with self.assertRaises(PermissionError):
                cat.discard(self.tmp, uid)
        self.assertEqual(len(cat.Catalog(self.tmp).entries()), 1)        # the catalog never lies

    def test_recent_collections(self):
        record(self.tmp, "inbox", "2026-10-05-a")
        b = record(self.tmp, "test", "2026-10-05-b")
        record(self.tmp, "bale", "2026-10-05-c")
        self.assertEqual(cat.Catalog(self.tmp).recent_collections(), ["bale", "test", "inbox"])
        cat.rename(self.tmp, b, "b moved", "demos")
        self.assertEqual(cat.Catalog(self.tmp).recent_collections(), ["demos", "bale", "inbox"])
        cat.discard(self.tmp, b)
        self.assertEqual(cat.Catalog(self.tmp).recent_collections(), ["bale", "inbox"])
        self.assertEqual(cat.Catalog(self.tmp).recent_collections(limit=1), ["bale"])

if __name__ == "__main__":
    unittest.main()
