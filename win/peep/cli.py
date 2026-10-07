"""Windows-side command line: what the WSL `peep` shim runs through
interop (`python.exe ...\\peep\\app\\peepw.py <command>`), and what can be
run directly from any Windows console.

  rec [slug] [--collection C] [--no-flash] [--audio system|mic|both|none] [--no-mic]
             [--scale WxH|native] [--encoder qsv|qsv-download|x264] [--fps N] [--stdin-stop line|off]
             [--no-render]       after `✓ saved`, the cut is rendered (render.auto) unless --no-render
  stop [--no-wait] [--timeout S]
  mark [--label TEXT]              drop a mark in the running recording (session B)
  pause [--no-wait] | resume [--no-wait]
                                   hard pause: capture stops; resume opens the next segment (C1a)
  take | retake                    open/close a take; discard the last take and open a fresh one (C1a)
  agent VERB                       the resident hotkey agent; see agentcli.py (session B)
  render [REF] [--force] [--dry-run]
                                   the cut, <stem>.cut.mp4, from the event record (session C1b)
  ls [--collection C] [--limit N] [--all] [--json]
  open [REF] [--raw] [--folder]    REF: last (default), a stem, or a uid prefix; the cut when it is
                                   current, else the original (every segment, as a playlist)
  rename REF NAME [--collection C]
  doctor [--capture]
  config [show|init|path]          show the effective config / write a template / where it is
  config get [KEY] | set KEY VALUE | unset KEY
                                   read or change one key; comments in config.toml are kept (A.1)
  paths                            where everything lives

Exit codes: 0 success, 1 failure (message on stderr), 2 usage error.

Sections:
  1. Parser                     (~line 44)
  2. Commands                   (~line 107)
  3. Entry point                (~line 369)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, agentcli, catalog, config as config_mod, configedit, naming, paths
from .control import AlreadyRecording, Control
from .logsetup import configured_log_path, event, setup_logging

log = logging.getLogger("peep.cli")

# ---------------------------------------------------------------------------
# 1. Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="peep", description="peep-peep screen recorder (Windows side)")
    p.add_argument("--version", action="version", version=f"peep {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    r = sub.add_parser("rec", help="record the screen (+ computer audio) until q/Enter or `peep stop`")
    r.add_argument("slug", nargs="?", help="name for the recording (default: from the foreground window)")
    r.add_argument("--collection", "-c", help="collection folder (default: config default_collection)")
    r.add_argument("--no-flash", action="store_true", help="skip the clapper flashes")
    r.add_argument("--audio", choices=config_mod.AUDIO_SOURCES,
                   help="audio for this recording: system (computer audio), mic, both or none "
                        "(default: config audio.sources)")
    r.add_argument("--no-mic", action="store_true",
                   help="leave the microphone out (both -> system, mic -> none; no effect on system)")
    r.add_argument("--scale", help="output size: native or WxH, e.g. 1920x1200")
    r.add_argument("--encoder", choices=config_mod.PIPELINES, help="video pipeline for this recording")
    r.add_argument("--fps", type=int, help="frame rate for this recording")
    r.add_argument("--stdin-stop", choices=("line", "off"), default="line",
                   help="line: any line on stdin, or stdin closing, stops the recording (default); "
                        "off: ignore stdin (for detached launches)")
    r.add_argument("--no-render", action="store_true",
                   help="do not render the cut after saving (config render.auto decides otherwise)")

    rd = sub.add_parser("render", help="render the cut (<stem>.cut.mp4) from the recording's event record")
    rd.add_argument("ref", nargs="?", default="last")
    rd.add_argument("--force", action="store_true", help="render again even when the cut is up to date")
    rd.add_argument("--dry-run", action="store_true",
                    help="decide every boundary and seam and print the plan and the ffmpeg command; write nothing")

    s = sub.add_parser("stop", help="stop the running recording")
    s.add_argument("--no-wait", action="store_true", help="return as soon as the request is written")
    s.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for the file to finalize")

    m = sub.add_parser("mark", help="drop a mark (cyan corner patch + sidecar entry) in the running recording")
    m.add_argument("--label", "-l", default="", help="optional text stored with the mark")

    for name, text in (("pause", "hard pause: capture stops and this segment is saved; `peep resume` continues"),
                       ("resume", "resume a paused recording: a new segment of the same recording starts")):
        pr = sub.add_parser(name, help=text)
        pr.add_argument("--no-wait", action="store_true", help="return as soon as the request is written")
        pr.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for it to take effect")
    sub.add_parser("take", help="open a take, or close the open one (only takes survive the cut)")
    sub.add_parser("retake", help="discard the most recent take (open or closed) and open a fresh one now")

    ls = sub.add_parser("ls", help="list recordings from the catalog")
    ls.add_argument("--collection", "-c")
    ls.add_argument("--limit", "-n", type=int, default=20)
    ls.add_argument("--all", action="store_true", help="include failed captures")
    ls.add_argument("--json", action="store_true")

    o = sub.add_parser("open", help="open a recording with the Windows default player")
    o.add_argument("ref", nargs="?", default="last")
    o.add_argument("--folder", action="store_true", help="open its folder in Explorer instead")
    o.add_argument("--raw", action="store_true", help="the original (segment 1), even when a cut exists")

    rn = sub.add_parser("rename", help="rename a recording (keeps its date prefix)")
    rn.add_argument("ref")
    rn.add_argument("name")
    rn.add_argument("--collection", "-c", help="also move it to this collection")

    d = sub.add_parser("doctor", help="check ffmpeg, devices, encoders, storage; prints fixes")
    d.add_argument("--capture", action="store_true", help="also run a 2 s capture into the null muxer")

    c = sub.add_parser("config", help="show or change the configuration (comments in the file are kept)")
    c.add_argument("action", nargs="?", default="show", choices=("show", "init", "path", "get", "set", "unset"),
                   help="show (default) | init | path | get [KEY] | set KEY VALUE | unset KEY")
    c.add_argument("key", nargs="?", help="dotted key, e.g. audio.offset_ms")
    c.add_argument("value", nargs="?", help="new value for set (typed by the key: 500, true, 1.5, text, a,b)")
    c.add_argument("--init", action="store_true", help="same as `config init`")

    sub.add_parser("paths", help="show where peep keeps things")
    agentcli.add_parser(sub)
    return p


# ---------------------------------------------------------------------------
# 2. Commands
# ---------------------------------------------------------------------------


def _out(msg: str = "") -> None:
    """Print a status line. A vanished terminal (the WSL tab closed mid-recording)
    must not abort the recording, so write failures are logged, never raised."""
    try:
        print(msg, flush=True)
    except (OSError, ValueError) as exc:
        event(log, logging.WARNING if not _out.failed else logging.DEBUG, "stdout.write_failed",
              error=repr(exc), message=msg)
        _out.failed = True


_out.failed = False


def _err(msg: str) -> None:
    try:
        print(f"peep: error: {msg}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        event(log, logging.ERROR, "stderr.write_failed", message=msg)


def _control(env=None) -> Control:
    from . import winapi
    return Control(paths.state_dir(env), winapi.pid_alive)


def stdin_fd() -> int | None:
    """The process's stdin file descriptor, or None when there is none to
    watch (pythonw.exe has sys.stdin None; a replaced stream may have no fd)."""
    if sys.stdin is None:
        return None
    try:
        return sys.stdin.fileno()
    except (OSError, ValueError, AttributeError) as exc:   # io.UnsupportedOperation is both
        event(log, logging.WARNING, "stdin.unwatchable", error=repr(exc))
        return None


def watch_stdin(stop_event: threading.Event, fd: int | None = None) -> threading.Thread | None:
    """Set `stop_event` on any input on stdin, or when stdin closes (the
    shim exiting or its terminal closing must stop the recording, not
    orphan it). Returns the watcher thread, or None if there is no stdin.

    Reads the raw descriptor with os.read, never sys.stdin/sys.stdin.buffer
    (session A.2). When the stop comes from elsewhere — `peep stop`, or the
    agent's hotkey on a terminal recording — this thread is still blocked in
    the read when the main thread returns, because the shim keeps the pipe
    open until the child exits. A BufferedReader holds its internal lock for
    the whole blocking read, and the interpreter's finalizer must take that
    lock to close sys.stdin: after a 1 s grace it aborts with "Fatal Python
    error: _enter_buffered_busy ... <stdin>" (exit code 3 on Windows), after
    the file was saved. os.read holds no Python-level lock, so finalization
    proceeds and the blocked daemon thread simply ends with the process.
    Any bytes count as a stop: the shim sends one 'stop' line, and a console
    read returns on Enter."""
    fd = stdin_fd() if fd is None else fd
    if fd is None:
        return None

    def run():
        try:
            data = os.read(fd, 4096)
            event(log, logging.INFO, "stdin.stop", eof=not data)
        except OSError as exc:      # EIO/EBADF: the terminal side vanished — a stop, not a crash
            event(log, logging.WARNING, "stdin.read_failed", error=repr(exc))
        stop_event.set()

    t = threading.Thread(target=run, name="stdin-stop", daemon=True)
    t.start()
    return t


def cmd_rec(args, cfg) -> int:
    from .recorder import RecordError, Recorder, RecordRequest
    if args.scale is not None:
        config_mod.parse_scale(args.scale)
    stop_event = threading.Event()
    if args.stdin_stop == "line":
        watch_stdin(stop_event)
    sources = config_mod.effective_sources(cfg.audio, args.audio, args.no_mic)
    req = RecordRequest(slug=args.slug, collection=args.collection,
                        flash=False if args.no_flash else None, audio_sources=sources,
                        pipeline=args.encoder, scale=args.scale, fps=args.fps)
    try:
        result = Recorder(cfg, _control(), status=_out).record(req, stop_event)
    except (RecordError, AlreadyRecording) as exc:
        _err(str(exc))
        return 1
    if result.ok:
        _out(f"✓ {result.message}")
        render_after_recording(cfg, result, skip=args.no_render)
        return 0
    _err(result.message)
    for line in result.stderr_tail[-8:]:
        print(f"    ffmpeg: {line}", file=sys.stderr)
    return 1


def progress_printer(step: float = 0.1):
    """A render progress callback for a terminal: the stage once, then every 10%."""
    state = {"stage": None, "next": step}

    def show(stage: str, fraction: float | None) -> None:
        if stage != state["stage"]:
            state["stage"] = stage
            if stage == "analysing":
                _out("  finding the flashes and patches, placing the seams…")
            elif stage == "encoding":
                _out("  encoding…")
        if stage == "encoding" and fraction is not None and fraction >= state["next"] - 1e-9:
            _out(f"  {int(fraction * 100)}%")
            state["next"] = (int(fraction / step) + 1) * step
    return show


def render_after_recording(cfg, result, skip: bool = False) -> bool | None:
    """Terminal recordings (session C1b): render the cut after `✓ saved`, in this
    process, printing progress. Whatever ended the recording (q, Enter, Ctrl-C,
    `peep stop`, the agent's hotkey) it is the same. A failure leaves the original
    untouched and says so; the recording's exit code stays 0, since it saved."""
    from . import render
    if skip:
        event(log, logging.INFO, "rec.render_skipped", why="--no-render", uid=result.uid)
        return None
    if not render.should_render(cfg.render.auto, result.summary, result.segments):
        event(log, logging.INFO, "rec.render_skipped", why=f"render.auto={cfg.render.auto}", uid=result.uid)
        return None
    if not result.uid:
        event(log, logging.INFO, "rec.render_skipped", why="the result names no recording")
        return None
    _out(f"✂ rendering the cut (render.auto = {cfg.render.auto}; `peep rec --no-render` skips it)")
    try:
        res = _renderer(cfg).render(result.uid, progress=progress_printer())
    except (render.RenderError, LookupError, ValueError, OSError) as exc:
        event(log, logging.ERROR, "rec.render_failed", uid=result.uid, error=str(exc))
        _err(f"render failed: {exc}. The recording is saved and untouched; `peep render` retries.")
        return False
    _out(f"✓ {res.message}")
    for line in res.fallbacks:
        _out(f"  ! {line}")
    return True


def _renderer(cfg):
    """A Renderer whose ffmpeg dies with this process (a kill-on-close job, as the
    recorder's): a `peep render` killed outright never leaves an encode running."""
    from . import render, winapi
    return render.Renderer(cfg, job_factory=winapi.KillOnCloseJob)


def cmd_render(args, cfg) -> int:
    """`peep render [REF] [--force] [--dry-run]` (session C1b)."""
    from . import render
    live = _control().live_recording() or {}
    try:
        res = _renderer(cfg).render(args.ref, force=args.force, dry_run=args.dry_run,
                                          live_uid=live.get("uid"),
                                          progress=None if args.dry_run else progress_printer())
    except render.RenderError as exc:
        _err(f"{exc}. The original is untouched.")
        return 1
    for line in res.report:
        _out(line)
    if res.dry_run or res.up_to_date:
        _out(res.message)
        return 0
    _out(f"✓ {res.message}")
    for line in res.fallbacks:
        _out(f"  ! {line}")
    return 0


def cmd_stop(args, cfg) -> int:
    ctl = _control()
    try:
        info = ctl.request_stop()
    except LookupError as exc:
        _err(str(exc))
        return 1
    _out(f"stop requested for {Path(info.get('final') or info.get('capture') or '?').name}")
    if args.no_wait:
        return 0
    if not ctl.wait_finished(int(info["pid"]), args.timeout):
        _err(f"recorder (pid {info['pid']}) has not finished after {args.timeout:g}s; check the log")
        return 1
    last = catalog.Catalog(cfg.root_path()).entries()
    mine = [e for e in last if e.uid == info.get("uid")]
    if mine:
        e = mine[-1]
        state = "saved" if e.status == "ok" else "FAILED, kept"
        _out(f"{state} {e.media_path(cfg.root_path())}")
        return 0 if e.status == "ok" else 1
    _err("the recorder finished but did not catalog a recording; check the log")
    return 1


def cmd_mark(args, cfg) -> int:
    try:
        _control().request_mark("cli", args.label)
    except LookupError as exc:
        _err(str(exc))
        return 1
    _out("◆ mark requested" + (f" ({args.label})" if args.label else ""))
    return 0


def cmd_pause_resume(args, cfg) -> int:
    """`peep pause` / `peep resume` (session C1a), for terminal recordings and any
    other: the same control-file request the agent's pause chord writes."""
    ctl = _control()
    kind = args.command
    try:
        info = ctl.request_event(kind, "cli")
    except LookupError as exc:
        _err(str(exc))
        return 1
    if args.no_wait:
        _out(f"{kind} requested")
        return 0
    want = ("paused",) if kind == "pause" else ("recording",)
    got = ctl.wait_status(int(info["pid"]), want, args.timeout)
    if got is None:
        now = ctl.read_active()
        if now is None:
            _err(f"the recording ended before it could {kind}")
        else:
            _err(f"{kind} requested, but the recording is still '{now.get('status')}' after {args.timeout:g}s "
                 f"(already {'paused' if kind == 'pause' else 'recording'}? see the log)")
        return 1
    if kind == "pause":
        _out(f"❚❚ paused after segment {got.get('segment')} ({format_duration(got.get('captured_s'))} captured, "
             f"saved). `peep resume` continues; `peep stop` ends the recording.")
    else:
        _out(f"● recording again: segment {got.get('segment')}")
    return 0


def cmd_take(args, cfg, wait_s: float = 3.0) -> int:
    """`peep take` / `peep retake` (session C1a). Once the recorder has consumed
    the request (it polls every 100 ms), reports the take count it published."""
    ctl = _control()
    kind = args.command
    try:
        req = ctl.request_event(kind, "cli")
    except LookupError as exc:
        _err(str(exc))
        return 1
    pid, path = int(req["pid"]), ctl.dir / req["file"]
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and path.exists():
        time.sleep(0.05)
    if path.exists():
        _out(f"{kind} requested (the recorder has not picked it up yet; it will)")
        return 0
    time.sleep(0.15)                     # the recorder publishes the count right after it acts
    now = ctl.read_active() or {}
    if int(now.get("pid", 0)) != pid:
        _out(f"{kind} requested (the recording has ended meanwhile)")
        return 0
    if now.get("status") in ("paused", "pausing", "resuming"):
        where = " (paused: it takes effect at the segment boundary)"
    else:
        where = ""
    state = "open" if now.get("take_open") else "closed"
    _out(f"{'↺ retake' if kind == 'retake' else '◉ take'}: {now.get('takes', 0)} take(s), the last one {state}{where}")
    return 0


def format_duration(seconds) -> str:
    if not isinstance(seconds, (int, float)):
        return "?"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def edit_info(root: Path, entry: catalog.Entry) -> dict:
    """What `peep ls` says about a recording's editing (session C1b): its
    segments, takes, and whether a cut exists and is current. Read from the
    sidecar; an unreadable one says so rather than guessing."""
    from . import render
    out = {"segments": 1, "takes": 0, "cut": None, "cut_state": "none"}
    if not entry.file:
        return out
    media = entry.media_path(root)
    try:
        sc = catalog.read_sidecar(catalog.sidecar_path(media))
    except FileNotFoundError:
        out["cut_state"] = "no sidecar"
        return out
    except (OSError, ValueError) as exc:
        event(log, logging.WARNING, "ls.sidecar_unreadable", file=entry.file, error=str(exc))
        out["cut_state"] = "sidecar unreadable"
        return out
    summary = sc.get("summary") or {}
    out["segments"] = len(sc.get("segments") or []) or 1
    out["takes"] = int(summary.get("takes") or 0)
    status = render.cut_status(sc, media.parent, naming.stem_of(media.name))
    out["cut_state"] = status.state
    if status.state in ("current", "stale"):
        out["cut"] = catalog.relpath_posix(root, status.path)
    return out


def edit_text(info: dict) -> str:
    parts = []
    if info.get("segments", 1) > 1:
        parts.append(f"{info['segments']} seg")
    if info.get("takes"):
        parts.append(f"{info['takes']} take{'s' if info['takes'] != 1 else ''}")
    state = info.get("cut_state")
    words = {"current": "cut", "stale": "cut (stale)", "failed": "render failed", "running": "rendering",
             "missing": "cut missing", "no sidecar": "no sidecar", "sidecar unreadable": "sidecar unreadable"}
    if state in words:
        parts.append(words[state])
    return " · ".join(parts)


def format_listing(entries: list[catalog.Entry], extras: dict | None = None) -> str:
    if not entries:
        return "(no recordings yet)"
    extras = extras or {}
    rows = [("created", "dur", "collection", "file")]
    for e in entries:
        mark = "" if e.status == "ok" else "  [failed]"
        info = edit_text(extras[e.uid]) if e.uid in extras else ""
        rows.append((e.created[:16].replace("T", " "), format_duration(e.duration_s), e.collection,
                     e.file + mark + (f"   [{info}]" if info else "")))
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    return "\n".join(f"{r[0]:<{widths[0]}}  {r[1]:>{widths[1]}}  {r[2]:<{widths[2]}}  {r[3]}" for r in rows)


def cmd_ls(args, cfg) -> int:
    entries = catalog.Catalog(cfg.root_path()).entries()
    if args.collection:
        coll = naming.validate_collection(args.collection)
        entries = [e for e in entries if e.collection == coll]
    if not args.all:
        entries = [e for e in entries if e.status == "ok"]
    entries = entries[-args.limit:] if args.limit > 0 else entries
    root = cfg.root_path()
    extras = {e.uid: edit_info(root, e) for e in entries}
    if args.json:
        _out(json.dumps([{**e.__dict__, **extras[e.uid]} for e in entries], indent=2, ensure_ascii=False))
    else:
        _out(format_listing(entries, extras))
    return 0


def open_target(cfg, entry: catalog.Entry, raw: bool = False) -> tuple[Path, str]:
    """What `peep open` plays (session C1b): the cut when it is current; else the
    original, every segment of it, as a playlist when there are several. Returns
    (path, a note for the user, possibly empty)."""
    from . import render
    root = cfg.root_path()
    media = entry.media_path(root)
    if raw:
        return media, ""
    try:
        sc = catalog.read_sidecar(catalog.sidecar_path(media))
    except (OSError, ValueError) as exc:
        event(log, logging.WARNING, "open.sidecar_unreadable", file=entry.file, error=str(exc))
        return media, ""
    stem = naming.stem_of(media.name)
    status = render.cut_status(sc, media.parent, stem)
    if status.current:
        return status.path, "  (the cut; `peep open --raw` for the original)"
    note = {"stale": "  (the cut is older than this recording's events: `peep render` updates it)",
            "failed": "  (its render failed: `peep render` retries, and says why)",
            "running": "  (the cut is still rendering)",
            "missing": "  (its cut is missing: `peep render` makes it again)"}.get(status.state, "")
    segs = [s for s in sc.get("segments") or [] if s.get("file") and (media.parent / s["file"]).exists()]
    if len(segs) > 1:
        playlist = paths.state_dir() / "playlists" / f"{stem}.m3u"
        playlist.parent.mkdir(parents=True, exist_ok=True)
        lines = ["#EXTM3U"] + [str(media.parent / s["file"]) for s in segs]
        playlist.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        event(log, logging.INFO, "open.playlist", path=str(playlist), segments=len(segs))
        return playlist, f"  ({len(segs)} segments in order; not rendered yet: `peep render` makes the cut)" + note
    return media, note


def cmd_open(args, cfg) -> int:
    from . import winapi
    entry = catalog.Catalog(cfg.root_path()).resolve(args.ref)
    media = entry.media_path(cfg.root_path())
    if not media.exists():
        _err(f"file is missing: {media}")
        return 1
    if args.folder:
        argv = ["explorer.exe", f"/select,{media}"]
        event(log, logging.INFO, "open.folder", argv=argv)
        subprocess.Popen(argv)
        _out(f"opened {media}")
        return 0
    target, note = open_target(cfg, entry, raw=args.raw)
    event(log, logging.INFO, "open.file", path=str(target), raw=args.raw)
    winapi.open_with_default(str(target))
    _out(f"opened {target}{note}")
    return 0


def cmd_rename(args, cfg) -> int:
    live = _control().live_recording() or {}
    entry = catalog.rename(cfg.root_path(), args.ref, args.name, args.collection, live_uid=live.get("uid"))
    _out(f"renamed → {entry.media_path(cfg.root_path())}"
         + ("  (that name was taken, so it got a counter)" if entry.suffixed else ""))
    return 0


def cmd_doctor(args, cfg) -> int:
    from .doctor import Doctor, render
    text, code = render(Doctor(cfg).checks(capture_test=args.capture))
    _out(text)
    return code


def cmd_config(args, cfg) -> int:
    p = paths.config_path()
    action = "init" if args.init else args.action
    if action in ("set", "unset") and not args.key:
        _err(f"config {action} needs a KEY (e.g. audio.offset_ms); `peep config get` lists them")
        return 2
    if action == "set" and args.value is None:
        _err(f"config set {args.key} needs a VALUE")
        return 2
    if action == "path":
        _out(str(p))
        return 0
    if action == "get":
        rows = configedit.get_rows(cfg, p, args.key)
        if args.key:
            _out(rows[0][1])
            return 0
        width = max(len(k) for k, _, _ in rows)
        for k, v, src in rows:
            _out(f"{k:<{width}} = {v}" + ("" if src == "default" else "   # set in config.toml"))
        return 0
    if action in ("set", "unset"):
        res = (configedit.set_value(p, args.key, args.value) if action == "set"
               else configedit.unset_value(p, args.key))
        event(log, logging.INFO, f"config.{action}", key=res.key, how=res.how, old=res.old, new=res.new, path=str(p))
        old, new = configedit.toml_literal(res.old), configedit.toml_literal(res.new)
        notes = {"replaced": "", "uncommented": " (uncommented the template line)", "added": " (added)",
                 "added-section": " (added, with its section)", "restored-comment": " (back to the commented default)",
                 "removed": " (line removed; default applies)", "not-set": " (was not set; default applies)"}
        _out(f"{res.key}: {old} -> {new}{notes[res.how]}" + (f"  [created {p}]" if res.created_file else ""))
        if res.how != "not-set":
            _out("  the running agent picks this up within ~2 s; `peep rec` uses it from the next recording")
        return 0
    if action == "init":
        if p.exists():
            _err(f"{p} already exists; edit it directly")
            return 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(config_mod.TEMPLATE, encoding="utf-8")
        _out(f"wrote {p}")
        return 0
    _out(f"# source: {cfg.source}  (file: {p})")
    _out(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False))
    return 0


def cmd_paths(args, cfg) -> int:
    rows = [("data dir", paths.data_dir()), ("config", paths.config_path()),
            ("log", configured_log_path() or paths.logs_dir() / "peep.log"),
            ("agent log", paths.logs_dir() / "agent.log"),
            ("state", paths.state_dir()), ("app (installed code)", paths.data_dir() / "app"),
            ("recordings root", cfg.root_path()), ("catalog", cfg.root_path() / catalog.CATALOG_NAME)]
    for k, v in rows:
        _out(f"{k:<22} {v}")
    return 0


COMMANDS = {"rec": cmd_rec, "stop": cmd_stop, "mark": cmd_mark, "pause": cmd_pause_resume,
            "resume": cmd_pause_resume, "take": cmd_take, "retake": cmd_take, "render": cmd_render,
            "ls": cmd_ls, "open": cmd_open,
            "rename": cmd_rename, "doctor": cmd_doctor, "config": cmd_config, "paths": cmd_paths,
            "agent": agentcli.cmd_agent}

# ---------------------------------------------------------------------------
# 3. Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        cfg = config_mod.load()
    except config_mod.ConfigError as exc:
        setup_logging(paths.logs_dir(), file_name=agentcli.log_file_for(args))
        event(log, logging.ERROR, "config.invalid", error=str(exc))
        _err(f"config: {exc}")
        if not (args.command == "config" and (args.init or args.action in ("set", "unset", "path"))):
            return 1
        # `peep config set/unset` is how a broken file gets fixed; they validate their own result
        _err("continuing so `config` can repair the file (built-in defaults shown meanwhile)")
        cfg = config_mod.Config()
    setup_logging(paths.logs_dir(), cfg.log_level, file_name=agentcli.log_file_for(args))
    event(log, logging.INFO, "cli.start", command=args.command, argv=sys.argv[1:] if argv is None else argv,
          version=__version__, config=cfg.source)
    try:
        code = COMMANDS[args.command](args, cfg)
    except (config_mod.ConfigError, naming.NamingError, LookupError, ValueError, OSError) as exc:
        # OSError covers FileNotFoundError/FileExistsError and Windows sharing violations
        # (a recording open in a player cannot be renamed: WinError 32).
        event(log, logging.ERROR, "cli.error", command=args.command, error=str(exc))
        _err(str(exc))
        code = 1
    except KeyboardInterrupt:
        event(log, logging.WARNING, "cli.interrupted", command=args.command)
        _err("interrupted")
        code = 1
    except Exception as exc:
        log.exception("unexpected error in %s", args.command)
        _err(f"unexpected {type(exc).__name__}: {exc} (traceback in {configured_log_path()})")
        code = 1
    event(log, logging.INFO, "cli.exit", command=args.command, exit_code=code)
    return code
