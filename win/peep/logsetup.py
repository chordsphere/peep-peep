"""Structured, leveled logging to a rotating file under the data directory.

Every record is one line: an ISO timestamp, the level, the logger name,
an event name, then key=value fields. Values are JSON-encoded whenever
they are not a bare token, so a line greps cleanly and parses back with
`parse_kv`. Use `event(log, level, name, **fields)` rather than
free-text messages for anything worth querying later (ffmpeg starts,
stops, exit codes, catalog writes).

Warnings are also echoed to stderr as `peep: warning: ...` so a problem
is never only in the log; errors are printed by the CLI itself.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path

LOG_FILE_NAME = "peep.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5
_BARE = re.compile(r"^[A-Za-z0-9_.:/\\@+-]+$")

_configured_path: Path | None = None


def format_value(value) -> str:
    """A field value as it appears in the log: bare when safe, JSON otherwise."""
    if isinstance(value, str) and value and _BARE.match(value):
        return value
    if isinstance(value, (int, float, bool)) and not isinstance(value, bool):
        return str(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def kv(name: str, **fields) -> str:
    parts = [name] + [f"{k}={format_value(v)}" for k, v in fields.items()]
    return " ".join(parts)


def event(log: logging.Logger, level: int, name: str, **fields) -> None:
    """Log one structured event: `name k=v k=v`."""
    log.log(level, kv(name, **fields))


_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\[.*?\](?=\s\w+=|$)|\{.*?\}(?=\s\w+=|$)|\S+)')


def parse_kv(message: str) -> tuple[str, dict]:
    """Inverse of `kv` for the common cases (bare tokens, JSON strings,
    flat JSON lists/objects). Used by tests and handy for log queries."""
    name, _, rest = message.partition(" ")
    fields = {}
    for key, raw in _PAIR.findall(rest):
        try:
            fields[key] = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            fields[key] = raw
    return name, fields


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return f"peep: {record.levelname.lower()}: {record.getMessage()}"


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> Path:
    """Configure the `peep` logger tree once per process; returns the log path.

    Creates `log_dir` if needed. If the directory cannot be created the
    failure is reported on stderr and logging continues to stderr only —
    a broken log location must not stop a recording, and must not be
    silent either.
    """
    global _configured_path
    root = logging.getLogger("peep")
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    path = Path(log_dir) / LOG_FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
                                                  encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root.addHandler(fh)
    except OSError as exc:
        print(f"peep: error: cannot open log file {path}: {exc}; logging to stderr only", file=sys.stderr)
        path = None
    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        # Warnings only while the file log works: every error path ends at the CLI,
        # which prints its own user-facing message, so echoing ERROR records too
        # would say it twice. Without a file, stderr is the only record: echo all.
        if path is not None:
            ch.addFilter(lambda record: record.levelno == logging.WARNING)
        ch.setFormatter(_ConsoleFormatter())
        root.addHandler(ch)
    _configured_path = path
    return path


def configured_log_path() -> Path | None:
    return _configured_path
