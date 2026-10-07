"""Names: kebab slugs, slug suggestion from the foreground window, and
collision-safe file stems. Pure functions — no clock, no filesystem
except through an injectable `exists` predicate — so session B's stop
dialog can prefill from `suggest_slug` and tests pin every rule.

Session C1a's naming audit tightened section 3: a stem is "taken" when any
file in the folder is that stem or starts with "<stem>." (compared
case-insensitively, as Windows does, even when the tests run on a
case-sensitive Linux filesystem), or when the catalog names a recording
with that stem in that collection, even one whose files are missing. Every
peep output for a recording (the media, segments, the sidecar, C1b's
`.cut.*` renders) lives in that "<stem>." family, so one reservation covers
them all. Section 1 refuses Windows-reserved names (CON, NUL, COM1, ...).

Sections:
  1. Kebab + validation         (~line 35)
  2. Slug suggestion            (~line 110)
  3. Stems + collisions         (~line 190)
"""

from __future__ import annotations

import datetime as _dt
import ntpath
import os
import re
import unicodedata
from typing import Callable, Iterable

# ---------------------------------------------------------------------------
# 1. Kebab + validation
# ---------------------------------------------------------------------------

MAX_SLUG = 48
FALLBACK_SLUG = "recording"
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_COLLECTION_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def kebab(text: str, max_len: int = MAX_SLUG) -> str:
    """Lowercase ASCII kebab-case, accents folded, runs of anything else
    collapsed to one '-', trimmed to `max_len` at a word boundary when
    one exists. May return "" (the caller decides the fallback)."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = _NON_ALNUM.sub("-", folded.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        slug = cut.rsplit("-", 1)[0] if "-" in cut[max_len // 2:] else cut
        slug = slug.strip("-")
    return slug


class NamingError(ValueError):
    """A user-supplied name or collection that cannot be used; says why."""


# Device names Windows reserves in every folder, with or without an extension
# ("con.mp4" is as unusable as "con"). The superscript digits are reserved too.
WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"]
    + [f"{dev}{n}" for dev in ("COM", "LPT") for n in list("123456789") + ["\u00b9", "\u00b2", "\u00b3"]])
_WINDOWS_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def windows_name_problem(name: str) -> str | None:
    """Why `name` cannot be a Windows file or folder name, or None if it can:
    a reserved device name (before the first dot, as Windows reads it), a
    trailing dot or space, a forbidden character, or nothing at all."""
    if not name:
        return "it is empty"
    if _WINDOWS_BAD_CHARS.search(name):
        return "it contains a character Windows does not allow in file names"
    if name[-1] in ". ":
        return "Windows does not allow a trailing dot or space"
    base = name.split(".", 1)[0].rstrip(" ").upper()
    if base in WINDOWS_RESERVED:
        return f"{base} is a device name Windows reserves"
    return None


def slug_from_user(name: str) -> str:
    """A name typed by the user (rename, `rec <slug>`, the dialog) as a slug,
    or raise: ASCII-folded, kebab-cased, capped at MAX_SLUG, and never a
    Windows-reserved name."""
    slug = kebab(name)
    if not slug:
        raise NamingError(f"{name!r} has no letters or digits to make a file name from")
    problem = windows_name_problem(slug)
    if problem:
        raise NamingError(f"{name!r} cannot be used as a name: {problem}")
    return slug


def validate_collection(name: str) -> str:
    """Collections are folder names: lowercase letters, digits, '-' and '_',
    no separators, no '..', never a Windows-reserved name ("con", "nul",
    "com1"). Returns the name normalised to lowercase."""
    norm = name.strip().lower()
    if not _COLLECTION_OK.match(norm):
        raise NamingError(f"collection {name!r} must be 1-64 chars of a-z, 0-9, '-', '_' "
                         f"and start with a letter or digit")
    problem = windows_name_problem(norm)
    if problem:
        raise NamingError(f"collection {name!r} cannot be used: {problem}")
    return norm


# ---------------------------------------------------------------------------
# 2. Slug suggestion
# ---------------------------------------------------------------------------

# Process stem (lowercase, no .exe) -> short app label used as the slug prefix.
APP_ALIASES = {
    "windowsterminal": "terminal", "wt": "terminal", "openconsole": "terminal",
    "conhost": "terminal", "cmd": "cmd", "powershell": "powershell", "pwsh": "powershell",
    "msedge": "edge", "chrome": "chrome", "firefox": "firefox", "brave": "brave",
    "code": "vscode", "code - insiders": "vscode", "cursor": "cursor",
    "explorer": "explorer", "notepad": "notepad", "obsidian": "obsidian",
    "winword": "word", "excel": "excel", "powerpnt": "powerpoint", "outlook": "outlook",
    "slack": "slack", "discord": "discord", "teams": "teams", "ms-teams": "teams",
    "applicationframehost": "app", "msrdc": "wslg",
}

# Title segments that only name the application ("... - Google Chrome").
APP_TITLE_SUFFIXES = {
    "google chrome", "microsoft edge", "mozilla firefox", "brave", "visual studio code",
    "notepad", "file explorer", "word", "excel", "powerpoint", "outlook", "slack",
    "discord", "microsoft teams", "obsidian", "cursor", "windows powershell", "command prompt",
    "windows terminal",
}

_TITLE_SPLIT = re.compile(r"\s+[-\u2013\u2014|]\s+")
_SHELL_PROMPT = re.compile(r"^(?:[\w.-]+@[\w.-]+:\s*)")     # "user@host: ~/dir"
_LEADING_MARKS = re.compile(r"^[\u25cf\u2022*\s]+")         # VS Code's unsaved dot, '*'
_ADMIN_PREFIX = re.compile(r"^(?:administrator|admin):\s*", re.I)


def app_label(process_image: str | None) -> str:
    """'C:\\...\\WindowsTerminal.exe' -> 'terminal'; unknown -> kebab of the stem."""
    if not process_image:
        return ""
    stem = ntpath.basename(process_image.replace("/", "\\"))
    if stem.lower().endswith(".exe"):
        stem = stem[:-4]
    return APP_ALIASES.get(stem.lower(), kebab(stem, 20))


def title_subject(title: str | None, app: str = "") -> str:
    """The informative part of a window title: app-name segments dropped,
    shell prompts reduced to the last path component."""
    if not title:
        return ""
    t = _ADMIN_PREFIX.sub("", _LEADING_MARKS.sub("", title.strip()))
    segments = [s.strip() for s in _TITLE_SPLIT.split(t) if s.strip()]
    keep = [s for s in segments
            if s.lower() not in APP_TITLE_SUFFIXES and (not app or kebab(s) != app)]
    if not keep:
        return ""
    subject = keep[0]
    if _SHELL_PROMPT.match(subject):
        subject = _SHELL_PROMPT.sub("", subject)
        subject = subject.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or subject
        if subject in ("~", ""):
            subject = "home"
    return subject


def suggest_slug(process_image: str | None, window_title: str | None, max_len: int = 40) -> str:
    """The slug B's stop dialog prefills: '<app>-<subject>', de-duplicated,
    kebab-cased, at most `max_len`; FALLBACK_SLUG when nothing is usable.

    >>> suggest_slug(r"C:\\Program Files\\WindowsApps\\WindowsTerminal.exe", "chordsphere@chordsphere: ~/peep-peep")
    'terminal-peep-peep'
    """
    app = app_label(process_image)
    subject = kebab(title_subject(window_title, app), max_len)
    if subject.startswith(app + "-") or subject == app:
        app = ""
    slug = kebab(f"{app} {subject}", max_len)
    if slug and windows_name_problem(slug):          # a window titled "con": never a device name
        slug = kebab(f"{FALLBACK_SLUG} {slug}", max_len)
    return slug or FALLBACK_SLUG


# ---------------------------------------------------------------------------
# 3. Stems + collisions
# ---------------------------------------------------------------------------

# A's list of the files a recording owns, by suffix relative to its stem; kept
# for the legacy `exists=` predicate. The C1a family below supersedes it.
OWNED_SUFFIXES = (".mp4", ".mkv", ".recording.mkv", ".json")
_STEM_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+?)(?:-(\d+))?$")

# Every file peep writes for a recording, as "<stem>.<rest>", <rest> matching
# this (case-insensitively): the capture and media of segment 1 and of every
# later segment (session C1a's hard pause), the sidecar and its write temp, and
# the `.cut.*` family reserved for session C1b's renders. Rename moves exactly
# these; discard deletes exactly these; nothing else in the folder is touched.
OWNED_REST = re.compile(r"^(?:(?:seg\d+\.)?(?:recording\.mkv|mkv|mp4)|json(?:\.tmp)?|cut(?:\.[a-z0-9]+)+)$",
                        re.IGNORECASE)


def segment_suffix(index: int, kind: str) -> str:
    """Where segment `index` (1-based) of a recording lives, relative to its stem.
    kind: "capture" (being written) | "mkv" (kept Matroska) | "mp4".
    Segment 1 keeps A's names exactly, so a session without a pause ends as today:
      1 -> .recording.mkv / .mkv / .mp4;   k >= 2 -> .seg<k>.recording.mkv / .seg<k>.mkv / .seg<k>.mp4"""
    ext = {"capture": ".recording.mkv", "mkv": ".mkv", "mp4": ".mp4"}[kind]
    return ext if index <= 1 else f".seg{index}{ext}"


def stem_of(file_name: str) -> str:
    """'2026-10-05-demo.seg2.mp4' -> '2026-10-05-demo'. Stems never contain a dot
    (slugs are kebab-case), so the stem is everything before the first one."""
    return file_name.split(".", 1)[0]


def owned_by(file_name: str, stem: str) -> bool:
    """Is `file_name` one of the files peep writes for `stem` (case-insensitive)?"""
    head, dot, rest = file_name.partition(".")
    return bool(dot) and head.casefold() == stem.casefold() and bool(OWNED_REST.match(rest))


def stem_for(date: _dt.date, slug: str, counter: int = 1) -> str:
    """'YYYY-MM-DD-<slug>' and, from counter 2 up, '-<n>' appended."""
    base = f"{date:%Y-%m-%d}-{slug}"
    return base if counter <= 1 else f"{base}-{counter}"


def stem_taken(directory: str, stem: str, exists: Callable[[str], bool] = os.path.exists,
               suffixes: Iterable[str] = OWNED_SUFFIXES) -> bool:
    """A's check through an `exists` predicate (kept for callers that inject one)."""
    return any(exists(os.path.join(directory, stem + s)) for s in suffixes)


def list_names(directory: str, listdir: Callable[[str], list[str]] = os.listdir) -> list[str]:
    """The folder's entries, [] when it does not exist yet."""
    try:
        return list(listdir(directory))
    except FileNotFoundError:
        return []


def taken_stems(names: Iterable[str]) -> set[str]:
    """Casefolded stems that the given file names occupy: a name is in the
    namespace of whatever precedes its first dot, so `x.mp4`, `x.json`,
    `x.seg2.mp4`, `x.cut.mp4`, even a hand-made `x.notes.txt`, all hold `x`."""
    return {stem_of(n).casefold() for n in names}


def allocate_stem(directory: str, date: _dt.date, slug: str,
                  exists: Callable[[str], bool] | None = None, limit: int = 999, *,
                  listdir: Callable[[str], list[str]] = os.listdir,
                  reserved: Iterable[str] = (), ignore: Iterable[str] = ()) -> str:
    """First free stem among <date>-<slug>, <date>-<slug>-2, -3, ...

    Free means: no entry of `directory` lies in its namespace (see
    `taken_stems`; compared case-insensitively, so `Demo.mp4` holds `demo`),
    and it is not in `reserved` (the catalog's stems for this collection,
    which hold their names even while their files are missing). `ignore`
    lists file names to disregard (a rename's own source files).
    `exists=` is A's older predicate interface: when given, only it is used."""
    if exists is not None:
        for n in range(1, limit + 1):
            stem = stem_for(date, slug, n)
            if not stem_taken(directory, stem, exists):
                return stem
        raise NamingError(f"more than {limit} recordings named {date:%Y-%m-%d}-{slug} in {directory}")
    skip = {i.casefold() for i in ignore}
    names = [n for n in list_names(directory, listdir) if n.casefold() not in skip]
    taken = taken_stems(names) | {r.casefold() for r in reserved}
    for n in range(1, limit + 1):
        stem = stem_for(date, slug, n)
        if stem.casefold() not in taken:
            return stem
    raise NamingError(f"more than {limit} recordings named {date:%Y-%m-%d}-{slug} in {directory}")


def split_stem(stem: str) -> tuple[str, str, int]:
    """'2026-10-05-demo-3' -> ('2026-10-05', 'demo', 3). A slug that itself
    ends in digits is ambiguous ('...-take-2'); the counter reading wins,
    which only matters for display. Raises if there is no date prefix."""
    m = _STEM_DATE.match(stem)
    if not m:
        raise NamingError(f"{stem!r} is not a peep stem (YYYY-MM-DD-slug)")
    return m.group(1), m.group(2), int(m.group(3) or 1)


def slug_of_stem(stem: str) -> str:
    """The stem without its date, counter included: what the file is actually
    called after the date ('2026-10-06-demo-2' -> 'demo-2'). The stop dialog
    prefills this, so it never offers a name that belongs to another recording."""
    m = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)$", stem)
    return m.group(1) if m else stem
