"""`peep config set KEY VALUE` / `get [KEY]` / `unset KEY`: change config.toml
without a text editor and without losing a word of its comments.

tomllib only reads, so this is a line-level edit of the file shape that
`config.TEMPLATE` defines: `[section]` headers, `key = value` lines, and the
template's commented-out defaults (`# key = value   # why`). The rules:

  set     an active `key = ...` line in the section gets its new value (its
          trailing comment and indentation kept); otherwise the template's
          commented line for the key is uncommented in place (this is the
          friction that hid the A/V offset for an hour: the value sat in a
          comment); otherwise the line is added at the end of its section,
          and the section is created if the file has none.
  unset   the active line goes back to the template's commented default line
          (or is removed when the template has none, or when that commented
          line is already in the file), so the built-in default applies.
  get     the effective value: the file's when set, the default otherwise.

Every edit is validated before anything is written: the new text must parse
with tomllib and load through `config.from_mapping`, the same loader the
recorder uses. A refused edit leaves the file byte-for-byte unchanged; an
accepted one is written atomically (temp file + os.replace), so the resident
agent's mtime reload (B #12) never sees half a file.

Sections:
  1. Keys and values            (~line 43)
  2. Line-level editing (pure)  (~line 142)
  3. The file                   (~line 311)
"""

from __future__ import annotations

import difflib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from .config import ConfigError

# ---------------------------------------------------------------------------
# 1. Keys and values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    dotted: str                 # "audio.offset_ms", or "root" for a top-level key
    section: str | None         # "audio", or None for a top-level key
    name: str
    type: object                # int | float | bool | str | tuple[str, ...]


def known_keys() -> dict[str, Key]:
    """Every key the loader accepts, from the config dataclasses themselves,
    so a key added to config.py is settable with no change here."""
    out = {}
    for name, typ in config_mod._TOP_KEYS.items():
        out[name] = Key(name, None, name, typ)
    for section, cls in config_mod._SECTIONS.items():
        for fname, typ in config_mod._field_types(cls).items():
            out[f"{section}.{fname}"] = Key(f"{section}.{fname}", section, fname, typ)
    return out


def lookup(dotted: str) -> Key:
    keys = known_keys()
    if dotted in keys:
        return keys[dotted]
    near = difflib.get_close_matches(dotted, list(keys), n=3, cutoff=0.6)
    hint = f"; did you mean {', '.join(near)}?" if near else "; `peep config get` lists every key"
    raise ConfigError(f"unknown config key {dotted!r}{hint}")


_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}


def parse_cli_value(key: Key, text: str):
    """The command-line VALUE typed by the key's type. Strings are taken
    verbatim (no quoting needed); lists are `a,b` or a TOML array."""
    t = key.type
    try:
        if t is bool:
            low = text.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ValueError
        if t is int:
            return int(text.strip())
        if t is float:
            return float(text.strip())
        if t == tuple[str, ...]:
            stripped = text.strip()
            if stripped.startswith("["):
                return list(tomllib.loads(f"v = {stripped}")["v"])
            return [p.strip() for p in stripped.split(",") if p.strip()]
        return text
    except (ValueError, tomllib.TOMLDecodeError):
        kind = {bool: "true/false", int: "an integer", float: "a number"}.get(t, "a list like a,b")
        raise ConfigError(f"{key.dotted} must be {kind}, got {text!r}") from None


def toml_literal(value) -> str:
    """A Python value as a one-line TOML literal ("(unreadable)" for None: a
    value that could not be read from a broken file)."""
    if value is None:
        return "(unreadable)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        return text if any(c in text for c in ".eEn") else text + ".0"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_literal(v) for v in value) + "]"
    if isinstance(value, str):
        out = ['"']
        for ch in value:
            if ch == '"':
                out.append('\\"')
            elif ch == "\\":
                out.append("\\\\")
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        out.append('"')
        return "".join(out)
    raise ConfigError(f"cannot write {value!r} as TOML")


# ---------------------------------------------------------------------------
# 2. Line-level editing (pure)
# ---------------------------------------------------------------------------

_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
_ACTIVE = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")
_COMMENTED = re.compile(r"^(\s*)#\s?([A-Za-z0-9_-]+)(\s*=\s*)(.*)$")


def split_value_comment(rest: str) -> tuple[str, str]:
    """`"a # b"   # note` -> ('"a # b"', '# note'): the value and the trailing
    comment, respecting basic and literal strings. Raises ConfigError for a
    value that continues past the line (multi-line strings and arrays),
    which this line editor will not touch."""
    i, n = 0, len(rest)
    depth = 0
    while i < n:
        ch = rest[i]
        if rest.startswith('"""', i) or rest.startswith("'''", i):
            raise ConfigError("multi-line strings are not edited by `peep config`; edit this key by hand")
        if ch == '"':
            i += 1
            while i < n and rest[i] != '"':
                i += 2 if rest[i] == "\\" else 1
            if i >= n:
                raise ConfigError("unterminated string on this line; edit it by hand")
        elif ch == "'":
            j = rest.find("'", i + 1)
            if j < 0:
                raise ConfigError("unterminated string on this line; edit it by hand")
            i = j
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "#":
            if depth > 0:
                raise ConfigError("a comment inside an array; edit this key by hand")
            return rest[:i].rstrip(), rest[i:]
        i += 1
    if depth > 0:
        raise ConfigError("multi-line arrays are not edited by `peep config`; edit this key by hand")
    return rest.rstrip(), ""


def _with_comment(code: str, comment: str, column: int | None) -> str:
    """`code` plus `comment`, the comment kept at `column` when it fits."""
    if not comment:
        return code
    if column is not None and len(code) < column:
        return code.ljust(column) + comment
    return code + "  " + comment


@dataclass
class _Line:
    index: int
    section: str | None
    kind: str                   # "header" | "active" | "commented" | "other"
    name: str | None = None


def _scan(lines: list[str]) -> list[_Line]:
    out, section = [], None
    for i, line in enumerate(lines):
        m = _HEADER.match(line)
        if m:
            section = m.group(1)
            out.append(_Line(i, section, "header", section))
            continue
        m = _ACTIVE.match(line)
        if m and not line.lstrip().startswith("#"):
            out.append(_Line(i, section, "active", m.group(2)))
            continue
        m = _COMMENTED.match(line)
        if m:
            out.append(_Line(i, section, "commented", m.group(2)))
            continue
        out.append(_Line(i, section, "other"))
    return out


def _comment_column(line: str) -> int | None:
    m = _ACTIVE.match(line) or _COMMENTED.match(line)
    if not m:
        return None
    try:
        value, comment = split_value_comment(m.group(4))
    except ConfigError:
        return None
    if not comment:
        return None
    return len(line) - len(m.group(4)) + m.group(4).index(comment)


def set_in_text(text: str, key: Key, literal: str) -> tuple[str, str]:
    """Return (new text, what happened: 'replaced' | 'uncommented' | 'added' | 'added-section')."""
    lines = text.splitlines()
    scan = _scan(lines)
    mine = [ln for ln in scan if ln.section == key.section and ln.name == key.name]
    active = [ln for ln in mine if ln.kind == "active"]
    if len(active) > 1:
        raise ConfigError(f"{key.dotted} is set {len(active)} times in the file; fix it by hand")
    if active:
        i = active[0].index
        m = _ACTIVE.match(lines[i])
        _, comment = split_value_comment(m.group(4))
        lines[i] = _with_comment(f"{m.group(1)}{key.name}{m.group(3)}{literal}", comment, _comment_column(lines[i]))
        how = "replaced"
    else:
        commented = [ln for ln in mine if ln.kind == "commented"]
        if commented:
            i = commented[0].index
            m = _COMMENTED.match(lines[i])
            _, comment = split_value_comment(m.group(4))
            lines[i] = _with_comment(f"{m.group(1)}{key.name} = {literal}", comment, _comment_column(lines[i]))
            how = "uncommented"
        else:
            entry = f"{key.name} = {literal}"
            in_section = [ln for ln in scan if ln.section == key.section]
            if key.section is None:
                first_header = next((ln.index for ln in scan if ln.kind == "header"), len(lines))
                body = [ln.index for ln in in_section if ln.kind in ("active", "commented") and ln.index < first_header]
                at = (body[-1] + 1) if body else first_header
                if at == first_header and at > 0 and lines[at - 1].strip() == "":
                    at -= 1
                lines.insert(at, entry)
                how = "added"
            elif any(ln.kind == "header" for ln in in_section):
                header = next(ln.index for ln in in_section if ln.kind == "header")
                body = [ln.index for ln in in_section if ln.kind in ("active", "commented")]
                at = (max(body) + 1) if body else header + 1
                lines.insert(at, entry)
                how = "added"
            else:
                if lines and lines[-1].strip():
                    lines.append("")
                lines += [f"[{key.section}]", entry]
                how = "added-section"
    return "\n".join(lines) + "\n", how


def template_line(key: Key, template: str) -> str | None:
    """The template's commented line for this key, verbatim, if it has one."""
    for ln in _scan(template.splitlines()):
        if ln.kind == "commented" and ln.section == key.section and ln.name == key.name:
            return template.splitlines()[ln.index]
    return None


def unset_in_text(text: str, key: Key, template: str = config_mod.TEMPLATE) -> tuple[str, str]:
    """Return (new text, 'restored-comment' | 'removed' | 'not-set')."""
    lines = text.splitlines()
    scan = _scan(lines)
    mine = [ln for ln in scan if ln.section == key.section and ln.name == key.name]
    active = [ln for ln in mine if ln.kind == "active"]
    if not active:
        return text, "not-set"
    restore = template_line(key, template)
    already_commented = any(ln.kind == "commented" for ln in mine)
    for ln in reversed(active):
        if restore is not None and not already_commented and ln is active[0]:
            lines[ln.index] = restore
        else:
            del lines[ln.index]
    how = "restored-comment" if (restore is not None and not already_commented) else "removed"
    return "\n".join(lines) + ("\n" if lines else ""), how


# ---------------------------------------------------------------------------
# 3. The file
# ---------------------------------------------------------------------------


def validate_text(text: str, path: Path, environ: dict | None = None) -> config_mod.Config:
    """Parse and load `text` exactly as `config.load` would load the file."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"the edit would make {path} invalid TOML: {exc}") from exc
    env = os.environ if environ is None else environ
    return config_mod.from_mapping(data, config_mod.Config(root=config_mod.default_root(env)), source=str(path))


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


@dataclass
class EditResult:
    key: str
    how: str                    # replaced | uncommented | added | added-section | restored-comment | removed | not-set
    old: object                 # effective value before
    new: object                 # effective value after
    created_file: bool = False


def _effective(cfg: config_mod.Config, key: Key):
    if key.section is None:
        return getattr(cfg, key.name)
    value = getattr(getattr(cfg, key.section), key.name)
    return list(value) if isinstance(value, tuple) else value


def set_value(path: Path, dotted: str, raw: str, environ: dict | None = None) -> EditResult:
    key = lookup(dotted)
    value = parse_cli_value(key, raw)
    created = not path.exists()
    text = config_mod.TEMPLATE if created else path.read_text(encoding="utf-8")
    try:
        old = _effective(validate_text(text, path, environ), key)
    except ConfigError:
        old = None                  # a broken file can be repaired by a set that makes it valid
    new_text, how = set_in_text(text, key, toml_literal(value))
    new_cfg = validate_text(new_text, path, environ)
    write_atomic(path, new_text)
    return EditResult(dotted, how, old, _effective(new_cfg, key), created)


def unset_value(path: Path, dotted: str, environ: dict | None = None) -> EditResult:
    key = lookup(dotted)
    if not path.exists():
        cfg = validate_text("", path, environ)
        return EditResult(dotted, "not-set", _effective(cfg, key), _effective(cfg, key))
    text = path.read_text(encoding="utf-8")
    try:
        old = _effective(validate_text(text, path, environ), key)
    except ConfigError:
        old = None
    new_text, how = unset_in_text(text, key)
    new_cfg = validate_text(new_text, path, environ)
    if how != "not-set":
        write_atomic(path, new_text)
    return EditResult(dotted, how, old, _effective(new_cfg, key))


def file_keys(path: Path) -> set[str]:
    """Dotted keys set (uncommented) in the file."""
    if not path.exists():
        return set()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out = set()
    for k, v in data.items():
        if isinstance(v, dict):
            out |= {f"{k}.{kk}" for kk in v}
        else:
            out.add(k)
    return out


def get_rows(cfg: config_mod.Config, path: Path, dotted: str | None = None) -> list[tuple[str, str, str]]:
    """(key, TOML value, 'file' | 'default') for one key or every key."""
    keys = [lookup(dotted)] if dotted else list(known_keys().values())
    in_file = file_keys(path)
    return [(k.dotted, toml_literal(_effective(cfg, k)), "file" if k.dotted in in_file else "default") for k in keys]
