"""Names: kebab slugs, slug suggestion from the foreground window, and
collision-safe file stems. Pure functions — no clock, no filesystem
except through an injectable `exists` predicate — so session B's stop
dialog can prefill from `suggest_slug` and tests pin every rule.

Sections:
  1. Kebab + validation         (~line 22)
  2. Slug suggestion            (~line 67)
  3. Stems + collisions         (~line 142)
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


def slug_from_user(name: str) -> str:
    """A name typed by the user (rename, `rec <slug>`) as a slug, or raise."""
    slug = kebab(name)
    if not slug:
        raise NamingError(f"{name!r} has no letters or digits to make a file name from")
    return slug


def validate_collection(name: str) -> str:
    """Collections are folder names: lowercase letters, digits, '-' and '_',
    no separators, no '..'. Returns the name normalised to lowercase."""
    norm = name.strip().lower()
    if not _COLLECTION_OK.match(norm):
        raise NamingError(f"collection {name!r} must be 1-64 chars of a-z, 0-9, '-', '_' "
                         f"and start with a letter or digit")
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
    return slug or FALLBACK_SLUG


# ---------------------------------------------------------------------------
# 3. Stems + collisions
# ---------------------------------------------------------------------------

# Every file a recording owns, by suffix relative to its stem. A stem is
# taken if any of these exists, so a capture in progress (.recording.mkv)
# or an orphaned sidecar still reserves its name.
OWNED_SUFFIXES = (".mp4", ".mkv", ".recording.mkv", ".json")
_STEM_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+?)(?:-(\d+))?$")


def stem_for(date: _dt.date, slug: str, counter: int = 1) -> str:
    """'YYYY-MM-DD-<slug>' and, from counter 2 up, '-<n>' appended."""
    base = f"{date:%Y-%m-%d}-{slug}"
    return base if counter <= 1 else f"{base}-{counter}"


def stem_taken(directory: str, stem: str, exists: Callable[[str], bool] = os.path.exists,
               suffixes: Iterable[str] = OWNED_SUFFIXES) -> bool:
    return any(exists(os.path.join(directory, stem + s)) for s in suffixes)


def allocate_stem(directory: str, date: _dt.date, slug: str,
                  exists: Callable[[str], bool] = os.path.exists, limit: int = 999) -> str:
    """First free stem among <date>-<slug>, <date>-<slug>-2, -3, ..."""
    for n in range(1, limit + 1):
        stem = stem_for(date, slug, n)
        if not stem_taken(directory, stem, exists):
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
