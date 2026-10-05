"""Storage core: per-recording sidecar JSON and the append-only
`catalog.jsonl` at the storage root.

On disk, under the root (default C:/Users/chord/Videos/peep):

    catalog.jsonl                         one event per line, append-only
    <collection>/<stem>.mp4               the recording
    <collection>/<stem>.json              its sidecar (schema SIDECAR_SCHEMA)

The catalog is the index: `ls` and `last` fold its events and never walk
directories. Events are `recorded` (a finished recording), `failed` (a
capture that did not finish cleanly; its files are kept for inspection)
and `renamed`. Every recording has a `uid` that survives renames, so the
fold keys on it.

The sidecar reserves `crop` and `marks` for session C (per-collection
crop rectangle, mark-based cuts) and records `trim: null` until C trims.

Sections:
  1. Sidecar                    (~line 44)
  2. Catalog events + fold      (~line 116)
  3. Rename                     (~line 223)
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import naming
from .logsetup import event

log = logging.getLogger("peep.catalog")

SIDECAR_SCHEMA = "peep.sidecar/1"
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
        "audio": None,              # {"device", "codec", "bitrate", "offset_ms"} or None when off
        "flash": {"enabled": False, "start": None, "stop": None},
        "timeline": {},             # agent-side ISO stamps + seconds since ffmpeg launch
        "ffmpeg": {"argv": None, "exit_code": None, "stderr_tail": None, "remux_argv": None},
        "trim": None,               # none applied yet (session C)
        "crop": None,               # RESERVED for session C: {"x","y","w","h"} in output pixels
        "marks": [],                # RESERVED for session C: [{"t": seconds, "label": str}]
    }


def sidecar_path(media_path: Path) -> Path:
    """`.../2026-10-05-demo.mp4` -> `.../2026-10-05-demo.json` (also for
    `.recording.mkv` captures)."""
    name = media_path.name
    stem = name[: -len(".recording.mkv")] if name.endswith(".recording.mkv") else media_path.stem
    return media_path.with_name(stem + ".json")


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
    with Path(path).open(encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("schema") != SIDECAR_SCHEMA:
        raise ValueError(f"{path}: unexpected sidecar schema {data.get('schema')!r}")
    return data


# ---------------------------------------------------------------------------
# 2. Catalog events + fold
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

    @property
    def stem(self) -> str:
        return Path(self.file).stem

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
        return list(state.values())

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


def relpath_posix(root: Path, path: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()


# ---------------------------------------------------------------------------
# 3. Rename
# ---------------------------------------------------------------------------


def rename(root: Path, ref: str, new_name: str, collection: str | None = None) -> Entry:
    """Rename a recording (and optionally move it to another collection).

    The date prefix of the original stem is kept; the slug comes from
    `new_name`; a collision gets the usual counter suffix. Media file and
    sidecar move together, the sidecar's title/slug/collection/file are
    updated, and a `renamed` event is appended. Nothing is overwritten:
    if a target appears between allocation and move, the move fails loudly.
    """
    cat = Catalog(root)
    entry = cat.resolve(ref)
    slug = naming.slug_from_user(new_name)
    dest_collection = naming.validate_collection(collection) if collection else entry.collection
    src_media = entry.media_path(root)
    if not src_media.exists():
        raise FileNotFoundError(f"recording file is missing: {src_media}")
    src_side = sidecar_path(src_media)
    date_str, _, _ = naming.split_stem(entry.stem)
    date = _dt.date.fromisoformat(date_str)
    dest_dir = Path(root) / dest_collection
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = naming.allocate_stem(str(dest_dir), date, slug,
                                exists=lambda p: os.path.exists(p) and Path(p).resolve() not in
                                {src_media.resolve(), src_side.resolve()})
    dest_media = dest_dir / (stem + src_media.suffix)
    dest_side = dest_dir / (stem + ".json")
    if dest_media == src_media:
        return entry
    for target in (dest_media, dest_side):
        if target.exists():
            raise FileExistsError(f"refusing to overwrite {target}")
    os.rename(src_media, dest_media)
    event(log, logging.INFO, "file.rename", src=str(src_media), dst=str(dest_media))
    if src_side.exists():
        data = read_sidecar(src_side)
        data.update(title=new_name.strip(), slug=slug, collection=dest_collection, file=dest_media.name)
        write_sidecar(dest_side, data)
        os.remove(src_side)
    else:
        event(log, logging.WARNING, "sidecar.missing", media=str(src_media))
    rel = relpath_posix(root, dest_media)
    cat.append({"event": "renamed", "uid": entry.uid, "from": entry.file, "to": rel,
                "collection": dest_collection, "title": new_name.strip()})
    entry.file, entry.collection, entry.title = rel, dest_collection, new_name.strip()
    return entry
