"""Storage core: per-recording sidecar JSON and the append-only
`catalog.jsonl` at the storage root.

On disk, under the root (default C:/Users/chord/Videos/peep):

    catalog.jsonl                         one event per line, append-only
    <collection>/<stem>.mp4               the recording
    <collection>/<stem>.json              its sidecar (schema SIDECAR_SCHEMA)

The catalog is the index: `ls` and `last` fold its events and never walk
directories. Events are `recorded` (a finished recording), `failed` (a
capture that did not finish cleanly; its files are kept for inspection),
`renamed`, and `discarded` (session B: the take was deleted on purpose;
the fold drops it). Every recording has a `uid` that survives renames, so
the fold keys on it.

The sidecar reserves `crop` for session C and records `trim: null` until C
trims. Session C1a made it `peep.sidecar/2`: a recording may now be several
segments (hard pause), and it carries the event record session C1b renders
from (segments, events, takes, pauses, summary). `/1` sidecars still load;
`as_v2` presents one as a one-segment, no-take recording.

Session C1a's naming audit: nothing here overwrites a file. New files are
created exclusively (`create_json_exclusive`), moves refuse an existing
target (`move_no_clobber`), and names are allocated against the folder
(case-insensitively) and the catalog together (`Catalog.stems_in`).

Sections:
  1. Sidecar                    (~line 60)
  2. Safe file operations       (~line 190)
  3. Catalog events + fold      (~line 250)
  4. Rename                     (~line 390)
  5. Discard                    (~line 520)
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import naming
from .logsetup import event

log = logging.getLogger("peep.catalog")

SIDECAR_SCHEMA = "peep.sidecar/2"
SIDECAR_SCHEMA_V1 = "peep.sidecar/1"
SIDECAR_SCHEMAS = (SIDECAR_SCHEMA_V1, SIDECAR_SCHEMA)
CATALOG_NAME = "catalog.jsonl"

# ---------------------------------------------------------------------------
# 1. Sidecar
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """Local time with UTC offset, millisecond precision."""
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def new_uid() -> str:
    return uuid.uuid4().hex


def new_sidecar(*, uid: str, slug: str, collection: str, file: str, created: str) -> dict:
    """A sidecar with every key present, so readers (B, C) never guess at
    absence. Fields the recorder fills later start as None."""
    return {
        "schema": SIDECAR_SCHEMA,
        "uid": uid,
        "title": slug,              # display title; equals slug until a rename supplies one
        "slug": slug,
        "collection": collection,
        "file": file,               # media file name, relative to the collection folder
        "created": created,
        "status": "recording",      # recording | ok | failed
        "duration_s": None,
        "foreground": {"title": None, "process": None, "image": None},
        "video": {"pipeline": None, "encoder": None, "fps": None, "output_idx": None,
                  "capture_size": None, "output_size": None, "container": None},
        "audio": None,              # {"device", "codec", "bitrate", "offset_ms"} or None when off; A.1 adds
                                    # sources, mix, tracks, epoch, system, mic, clap (recorder._audio_block)
        "flash": {"enabled": False, "start": None, "stop": None},
        "timeline": {},             # agent-side ISO stamps + seconds since ffmpeg launch
        "ffmpeg": {"argv": None, "exit_code": None, "stderr_tail": None, "remux_argv": None},
        "trim": None,               # none applied yet (session C)
        "crop": None,               # RESERVED for session C: {"x","y","w","h"} in output pixels
        "marks": [],                # session B: [{"t", "label", "source", "flash", ...}]; C1a adds "segment"
        # -- peep.sidecar/2 (session C1a): the event record session C1b renders from.
        # The top-level video/audio/flash/timeline/ffmpeg blocks above describe segment 1
        # (the file the catalog points at), so a one-segment recording reads exactly as /1.
        "segments": [],             # one block per capture segment, in order (events.segment_block)
        "events": [],               # every take / retake / pause / resume / mark press, accepted or ignored
        "takes": [],                # derived from events: [{"id", "status": kept|discarded, "open", "close", ...}]
        "pauses": [],               # [{"after_segment", "pause_event", "resume_event", "paused_qpc", ...}]
        "summary": None,            # {"segments", "takes", "takes_discarded", "kept_s", "total_s", "whole"}
    }


def sidecar_path(media_path: Path) -> Path:
    """`.../2026-10-05-demo.mp4` -> `.../2026-10-05-demo.json`, for every file of
    the recording (`.recording.mkv` captures, `.seg2.mp4` segments, `.cut.mp4`)."""
    return media_path.with_name(naming.stem_of(media_path.name) + ".json")


def write_json_atomic(path: Path, data: dict) -> None:
    """Write via a temp file + os.replace so a crash never leaves half a sidecar."""
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def write_sidecar(path: Path, data: dict) -> None:
    write_json_atomic(path, data)
    event(log, logging.INFO, "sidecar.write", path=str(path), uid=data.get("uid"), status=data.get("status"))


def read_sidecar(path: Path) -> dict:
    """The sidecar as stored (`peep.sidecar/1` or `/2`); see `as_v2` for one shape."""
    with Path(path).open(encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("schema") not in SIDECAR_SCHEMAS:
        raise ValueError(f"{path}: unexpected sidecar schema {data.get('schema')!r}")
    return data


def as_v2(data: dict) -> dict:
    """A `peep.sidecar/2` view of any sidecar (a copy; the input is untouched).

    Migration note: a `/1` sidecar is a single capture with no events, so its
    view is one segment built from the top-level blocks (file, duration,
    flashes, timeline, video, audio, ffmpeg), its marks placed in segment 1,
    no takes and no pauses, and a summary that keeps the whole recording.
    `/1` files on disk are never rewritten to `/2`; a rename keeps the version."""
    if data.get("schema") == SIDECAR_SCHEMA:
        return data
    if data.get("schema") != SIDECAR_SCHEMA_V1:
        raise ValueError(f"unexpected sidecar schema {data.get('schema')!r}")
    out = json.loads(json.dumps(data))
    flash, timeline = out.get("flash") or {}, out.get("timeline") or {}
    dur = out.get("duration_s")
    epoch = (out.get("audio") or {}).get("epoch") or {}
    out["schema"] = SIDECAR_SCHEMA
    out["segments"] = [{
        "index": 1, "file": out.get("file"), "status": out.get("status"), "duration_s": dur,
        "started_at": timeline.get("ffmpeg_started_at"), "ffmpeg_started_qpc": timeline.get("ffmpeg_started_qpc"),
        "input0_qpc": timeline.get("input0_qpc"), "video_epoch_qpc_est": epoch.get("video_epoch_qpc_est"),
        "stop_reason": timeline.get("stop_reason"), "flash": {"start": flash.get("start"), "stop": flash.get("stop")},
        "timeline": timeline, "video": out.get("video"), "audio": out.get("audio"), "ffmpeg": out.get("ffmpeg"),
        "migrated_from": SIDECAR_SCHEMA_V1}]
    for m in out.get("marks") or []:
        m.setdefault("segment", 1)
    out["events"], out["takes"], out["pauses"] = [], [], []
    out["summary"] = {"segments": 1, "takes": 0, "takes_discarded": 0, "kept_s": dur, "total_s": dur, "whole": True}
    return out


# ---------------------------------------------------------------------------
# 2. Safe file operations (session C1a's naming audit)
# ---------------------------------------------------------------------------


def create_json_exclusive(path: Path, data: dict) -> None:
    """Create `path` with `data`, refusing (FileExistsError) if it exists: the
    sidecar that reserves a new recording's stem is born this way, so a name
    taken between allocation and creation is never overwritten. Later
    rewrites of our own sidecar use `write_sidecar` (temp file + replace)."""
    with Path(path).open("x", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    event(log, logging.INFO, "sidecar.create", path=str(path), uid=data.get("uid"))


def name_in_folder(folder: Path, name: str, listdir=os.listdir) -> str | None:
    """The entry of `folder` that is `name` compared case-insensitively (what
    Windows would open), or None."""
    want = name.casefold()
    for n in naming.list_names(str(folder), listdir):
        if n.casefold() == want:
            return n
    return None


def move_no_clobber(src: Path, dst: Path) -> None:
    """Move/rename `src` to `dst`, never replacing anything: FileExistsError if
    `dst` exists, under any capitalisation. On Windows os.rename already
    refuses an existing target; on POSIX a hard link (which also refuses) then
    an unlink, falling back to a checked rename where links are unsupported."""
    src, dst = Path(src), Path(dst)
    clash = name_in_folder(dst.parent, dst.name)
    if clash is not None and not (dst.parent / clash).samefile(src):
        raise FileExistsError(f"refusing to overwrite {dst.parent / clash}")
    if os.name == "nt":
        os.rename(src, dst)
    else:
        try:
            os.link(src, dst)
        except FileExistsError:
            raise FileExistsError(f"refusing to overwrite {dst}") from None
        except OSError as exc:            # no hard links here (EPERM/EXDEV/ENOTSUP): checked rename
            event(log, logging.DEBUG, "file.link_unsupported", src=str(src), error=repr(exc))
            if os.path.lexists(dst):
                raise FileExistsError(f"refusing to overwrite {dst}") from None
            os.rename(src, dst)
        else:
            os.unlink(src)
    event(log, logging.INFO, "file.rename", src=str(src), dst=str(dst))


# ---------------------------------------------------------------------------
# 3. Catalog events + fold
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    """The current state of one recording, folded from catalog events."""
    uid: str
    collection: str
    file: str          # path relative to root, forward slashes
    title: str
    created: str
    duration_s: float | None
    status: str        # ok | failed
    suffixed: bool = False     # set by rename(): the wanted name was taken, a counter was added

    @property
    def stem(self) -> str:
        return naming.stem_of(Path(self.file).name)

    def media_path(self, root: Path) -> Path:
        return Path(root) / Path(self.file)


class Catalog:
    """`catalog.jsonl` under a storage root. Appends are one line each,
    flushed and fsynced; reading tolerates (and reports) a torn last line."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / CATALOG_NAME

    def append(self, record: dict) -> None:
        record = {"ts": now_iso(), **record}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self.root.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        event(log, logging.INFO, "catalog.append", path=str(self.path), ev=record.get("event"),
              uid=record.get("uid"), file=record.get("file") or record.get("to"))

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    event(log, logging.WARNING, "catalog.bad_line", path=str(self.path), line=lineno,
                          error=str(exc))
        return out

    def entries(self) -> list[Entry]:
        """Current recordings, oldest first (by first appearance in the catalog)."""
        state: dict[str, Entry] = {}
        for ev in self.events():
            kind, uid = ev.get("event"), ev.get("uid")
            if not uid:
                continue
            if kind in ("recorded", "failed"):
                state[uid] = Entry(uid=uid, collection=ev.get("collection", ""), file=ev.get("file", ""),
                                   title=ev.get("title", ""), created=ev.get("created", ""),
                                   duration_s=ev.get("duration_s"),
                                   status="ok" if kind == "recorded" else "failed")
            elif kind == "renamed" and uid in state:
                e = state[uid]
                e.file = ev.get("to", e.file)
                e.collection = ev.get("collection", e.collection)
                e.title = ev.get("title", e.title)
            elif kind == "discarded":
                state.pop(uid, None)
        return list(state.values())

    def stems_in(self, collection: str, exclude_uid: str | None = None) -> set[str]:
        """Stems the catalog's current recordings hold in `collection`, failed
        ones included and whether or not their files still exist: the naming
        audit treats them as taken, so a recording whose file was moved away
        by hand is never given a twin. Compared case-insensitively."""
        coll = collection.casefold()
        return {naming.stem_of(Path(e.file).name) for e in self.entries()
                if e.file and e.uid != exclude_uid and e.collection.casefold() == coll}

    def recent_collections(self, limit: int = 10) -> list[str]:
        """Collections that hold recordings now, most recently active first
        (a recording's latest event, a rename included, dates its current
        collection). A collection emptied by moves or discards drops out.
        For the stop dialog's picker."""
        current = {e.uid: e.collection for e in self.entries()}
        out: list[str] = []
        for ev in reversed(self.events()):
            coll = current.get(ev.get("uid"))
            if coll and coll not in out:
                out.append(coll)
        return out[:limit]

    def last(self, include_failed: bool = False) -> Entry | None:
        """The most recently recorded entry (by catalog order, not rename order)."""
        for e in reversed(self.entries()):
            if include_failed or e.status == "ok":
                return e
        return None

    def resolve(self, ref: str) -> Entry:
        """'last', a stem, a relative file path, or a uid prefix (>= 6 chars)."""
        entries = self.entries()
        if ref == "last":
            e = self.last()
            if e is None:
                raise LookupError(f"no recordings in {self.path}")
            return e
        matches = [e for e in entries if ref in (e.stem, e.file, Path(e.file).name)]
        if not matches and len(ref) >= 6:
            matches = [e for e in entries if e.uid.startswith(ref)]
        if not matches:
            raise LookupError(f"no recording matches {ref!r}")
        if len(matches) > 1:
            raise LookupError(f"{ref!r} is ambiguous: " + ", ".join(e.file for e in matches))
        return matches[0]


_UID = re.compile(r"^[0-9a-f]{32}$")


class RecordingInProgress(ValueError):
    """The recording is still live (recording or paused): its files are being
    written, so it cannot be renamed or discarded yet."""


def resolve_ref(cat: Catalog, ref) -> Entry:
    """An Entry, or anything a user or the agent names a recording by: 'last', a
    stem, a file, a uid prefix, or a full uid (32 hex digits; tried as a uid
    first, then as a name, so a 32-character stem still resolves)."""
    if isinstance(ref, Entry):
        return ref
    ref = str(ref)
    if _UID.match(ref):
        try:
            return _resolve_uid(cat, ref)
        except LookupError:
            pass
    return cat.resolve(ref)


def refuse_if_live(entry: Entry, live_uid: str | None, doing: str) -> None:
    if live_uid and entry.uid == live_uid:
        raise RecordingInProgress(f"{entry.file or entry.title} is still recording (or paused); "
                                  f"{doing} it after it stops")


def relpath_posix(root: Path, path: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


# ---------------------------------------------------------------------------
# 4. Rename
# ---------------------------------------------------------------------------


@dataclass
class RenamePlan:
    """What a rename will do, computed before anything moves. The stop dialog
    shows `final_name` live as the user types; `rename` then executes the plan.
    `suffixed` is True when the wanted name is taken and a counter was added."""
    entry: Entry
    title: str
    slug: str
    collection: str
    stem: str                  # the stem the recording will have
    wanted_stem: str           # <date>-<slug> before any counter
    src_stem: str
    noop: bool                 # same folder, same stem: nothing to move

    @property
    def suffixed(self) -> bool:
        return self.stem != self.wanted_stem and not self.noop

    def final_name(self) -> str:
        """`collection/stem.ext` as the catalog will list it."""
        ext = Path(self.entry.file).name[len(self.src_stem):]
        return f"{self.collection}/{self.stem}{ext}"


def plan_rename(root: Path, ref, new_name: str, collection: str | None = None,
                cat: Catalog | None = None, live_uid: str | None = None) -> RenamePlan:
    """Resolve, validate and allocate a rename without touching any file.
    `ref` is an Entry or anything `resolve_ref` takes (for the dialog, the uid).
    `live_uid` is the recording in progress (active.json): it cannot be renamed
    while its segments are still being written."""
    cat = cat or Catalog(root)
    entry = resolve_ref(cat, ref)
    refuse_if_live(entry, live_uid, "rename")
    slug = naming.slug_from_user(new_name)
    dest_collection = naming.validate_collection(collection) if collection else entry.collection
    if not entry.file:
        raise FileNotFoundError(f"recording {entry.uid[:8]} has no file (its capture never started)")
    src_name = Path(entry.file).name
    src_stem = naming.stem_of(src_name)
    date_str, _, _ = naming.split_stem(src_stem)
    date = _dt.date.fromisoformat(date_str)
    same_folder = dest_collection.casefold() == entry.collection.casefold()
    dest_dir = Path(root) / dest_collection
    src_dir = Path(root) / Path(entry.file).parent
    ignore = [n for n in naming.list_names(str(src_dir)) if naming.owned_by(n, src_stem)] if same_folder else []
    stem = naming.allocate_stem(str(dest_dir), date, slug, reserved=cat.stems_in(dest_collection, entry.uid),
                                ignore=ignore)
    return RenamePlan(entry=entry, title=new_name.strip(), slug=slug, collection=dest_collection, stem=stem,
                      wanted_stem=naming.stem_for(date, slug), src_stem=src_stem,
                      noop=same_folder and stem.casefold() == src_stem.casefold())


def _family(folder: Path, stem: str) -> list[str]:
    """The files peep owns for `stem` in `folder` (naming.OWNED_REST), the
    sidecar last so a half-finished move never strands media without one."""
    names = [n for n in naming.list_names(str(folder)) if naming.owned_by(n, stem)
             and not n.casefold().endswith(".json.tmp")]
    return sorted(names, key=lambda n: (n.casefold().endswith(".json"), n))


def rename(root: Path, ref: str, new_name: str, collection: str | None = None,
           live_uid: str | None = None) -> Entry:
    """Rename a recording (and optionally move it to another collection).

    The date prefix of the original stem is kept; the slug comes from
    `new_name`; a name already taken in the target folder (case-insensitively)
    or held by another catalogued recording gets the usual counter suffix,
    and `entry.suffixed` says so. Every file of the recording moves together
    (media, later segments, the sidecar, any `.cut.*` render), each one
    refusing to overwrite; if one cannot move (a player holding it), the ones
    already moved are moved back and the error is raised. Then the sidecar's
    title/slug/collection/file and segment file names are updated and a
    `renamed` event is appended."""
    root = Path(root)
    plan = plan_rename(root, ref, new_name, collection, live_uid=live_uid)
    entry = plan.entry
    src_media = entry.media_path(root)
    if not src_media.exists():
        raise FileNotFoundError(f"recording file is missing: {src_media}")
    entry.suffixed = False
    if plan.noop:
        return entry
    src_dir, dest_dir = src_media.parent, root / plan.collection
    dest_dir.mkdir(parents=True, exist_ok=True)
    names = _family(src_dir, plan.src_stem)
    moves = [(src_dir / n, dest_dir / (plan.stem + n[len(plan.src_stem):])) for n in names]
    for _, dst in moves:                  # refuse before moving anything
        clash = name_in_folder(dest_dir, dst.name)
        if clash is not None and naming.stem_of(clash).casefold() != plan.src_stem.casefold():
            raise FileExistsError(f"refusing to overwrite {dest_dir / clash}")
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in moves:
            move_no_clobber(src, dst)
            done.append((src, dst))
    except OSError as exc:
        event(log, logging.ERROR, "rename.failed_midway", moved=len(done), of=len(moves), error=repr(exc))
        for src, dst in reversed(done):
            try:
                move_no_clobber(dst, src)
            except OSError as back:
                event(log, logging.ERROR, "rename.rollback_failed", src=str(dst), dst=str(src), error=repr(back))
        raise
    dest_media = dest_dir / (plan.stem + src_media.name[len(plan.src_stem):])
    dest_side = dest_dir / (plan.stem + ".json")
    if dest_side.exists():
        data = read_sidecar(dest_side)
        data.update(title=plan.title, slug=plan.slug, collection=plan.collection, file=dest_media.name)
        for seg in data.get("segments") or []:
            for key in ("file", "capture_file"):
                if isinstance(seg.get(key), str) and naming.stem_of(seg[key]).casefold() == plan.src_stem.casefold():
                    seg[key] = plan.stem + seg[key][len(plan.src_stem):]
        write_sidecar(dest_side, data)
    else:
        event(log, logging.WARNING, "sidecar.missing", media=str(src_media))
    rel = relpath_posix(root, dest_media)
    cat = Catalog(root)
    cat.append({"event": "renamed", "uid": entry.uid, "from": entry.file, "to": rel,
                "collection": plan.collection, "title": plan.title,
                **({"suffixed_from": plan.wanted_stem} if plan.suffixed else {})})
    if plan.suffixed:
        event(log, logging.INFO, "rename.suffixed", wanted=plan.wanted_stem, got=plan.stem)
    entry.file, entry.collection, entry.title = rel, plan.collection, plan.title
    entry.suffixed = plan.suffixed
    return entry


# ---------------------------------------------------------------------------
# 5. Discard
# ---------------------------------------------------------------------------


def discard(root: Path, ref: str, reason: str = "user", live_uid: str | None = None) -> Entry:
    """Delete a recording on purpose (session B's discard hotkey / dialog):
    every file peep owns for it goes (media, later segments, sidecar, any
    leftover capture or render; naming.OWNED_REST, nothing else in the
    folder), and a `discarded` event is appended so the fold forgets it. A
    file already missing is logged, not fatal: the intent is "this take is
    gone". A file that cannot be deleted (open in a player: WinError 32)
    raises before the event is written, so the catalog never claims a
    deletion that did not happen."""
    cat = Catalog(root)
    entry = resolve_ref(cat, ref)
    refuse_if_live(entry, live_uid, "discard")
    targets: list[Path] = []
    if entry.file:
        media = entry.media_path(root)
        stem = naming.stem_of(media.name)
        targets = [media.parent / n for n in naming.list_names(str(media.parent)) if naming.owned_by(n, stem)]
    removed = []
    for t in sorted(targets):
        if t.exists():
            os.remove(t)
            removed.append(t.name)
            event(log, logging.INFO, "file.discard", path=str(t))
    if not removed:
        event(log, logging.WARNING, "discard.nothing_on_disk", uid=entry.uid, file=entry.file)
    cat.append({"event": "discarded", "uid": entry.uid, "file": entry.file, "collection": entry.collection,
                "title": entry.title, "reason": reason, "removed": removed})
    return entry


def _resolve_uid(cat: Catalog, uid: str) -> Entry:
    for e in cat.entries():
        if e.uid == uid:
            return e
    raise LookupError(f"no recording with uid {uid}")
