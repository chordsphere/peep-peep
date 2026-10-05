"""Process control and the record flow.

`FfmpegProcess` owns one ffmpeg child: starts it (argv logged verbatim
first), drains its stderr into the log and a tail buffer, detects
readiness, stops it by writing `q` to its stdin (interop does not
forward Ctrl-C reliably; a Windows pipe does), and surfaces exit code
and stderr tail on failure.

`Recorder.record()` is the whole recording, start to catalog:

  name    foreground window -> slug (unless given) -> collision-free stem
  claim   sidecar written at once (status "recording", reserves the stem),
          control file claimed so `peep stop` can find us
  start   ffmpeg launched into <stem>.recording.mkv; if the pipeline dies
          during start-up, the next one in video.fallback is tried, loudly
  flash   start flash once ffmpeg reports its output open (+ lead_ms)
  wait    until stop_event (q/Enter in the terminal, or the agent's hotkey), a
          stop-request file (`peep stop`), or ffmpeg exiting on its own (a
          failure); meanwhile mark requests (`peep mark`, the agent's mark
          hotkey) flash the mark colour and land in the sidecar's `marks`
  stop    stop flash, settle_ms, then `q`; escalate to terminate on timeout
  final   remux to <stem>.mp4 (streams copied) and drop the capture, or
          keep the Matroska; sidecar completed; catalog event appended

Matroska is the capture container because a capture cut short by a
crash or power loss is still playable; MP4 is only produced from a
cleanly closed capture.

Sections:
  1. FfmpegProcess              (~line 56)
  2. Request / result           (~line 193)
  3. Recorder                   (~line 224)
"""

from __future__ import annotations

import collections
import datetime as _dt
import logging
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import catalog, ffmpeg_cmd, naming
from .config import Config
from .flash import FlashRecord, NullFlasher
from .logsetup import event
from .winapi import ForegroundInfo

log = logging.getLogger("peep.recorder")

# ---------------------------------------------------------------------------
# 1. FfmpegProcess
# ---------------------------------------------------------------------------

READY_MARKERS = ("Output #0",)          # printed once the muxer header is written: capture is live
_TIME = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_CAPTURE_SIZE = re.compile(r"Stream #0:0.*?Video:.*?(\d{3,5})x(\d{3,5})")
CREATE_NO_WINDOW = 0x08000000


class FfmpegProcess:
    def __init__(self, popen: Callable = subprocess.Popen, tail_lines: int = 200, job=None):
        self._popen_factory = popen
        self.proc = None
        self.argv: list[str] = []
        self.tail = collections.deque(maxlen=tail_lines)
        self.ready = threading.Event()
        self.started_mono: float | None = None   # time.monotonic() at launch: the t0 of the timeline
        self.started_at: str | None = None        # the same instant as local ISO time
        self._job = job
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, argv: list[str]) -> None:
        self.argv = list(argv)
        event(log, logging.INFO, "ffmpeg.argv", argv=self.argv, cmdline=subprocess.list2cmdline(self.argv))
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        self.started_mono = time.monotonic()
        self.started_at = _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")
        self.proc = self._popen_factory(self.argv, **kwargs)
        event(log, logging.INFO, "ffmpeg.start", pid=getattr(self.proc, "pid", None))
        if self._job is not None:
            self._job.assign(self.proc)
        self._reader = threading.Thread(target=self._drain, name="ffmpeg-stderr", daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        stream = self.proc.stderr
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                with self._lock:
                    self.tail.append(line)
                log.debug("ffmpeg| %s", line)
                if not self.ready.is_set() and any(m in line for m in READY_MARKERS):
                    self.ready.set()
        except (OSError, ValueError) as exc:
            event(log, logging.WARNING, "ffmpeg.stderr_read_failed", error=repr(exc))

    def wait_ready(self, timeout_s: float, poll_s: float = 0.05,
                   clock: Callable[[], float] = time.monotonic) -> str:
        """'ready' once the output is open, 'exited' if ffmpeg died first,
        'timeout' if neither happened in time (ffmpeg still running)."""
        deadline = clock() + timeout_s
        while clock() < deadline:
            if self.ready.wait(poll_s):
                return "ready"
            if self.poll() is not None:
                self.join_reader()
                return "ready" if self.ready.is_set() and self.poll() == 0 else "exited"
        return "timeout"

    def poll(self):
        return self.proc.poll() if self.proc else None

    def send_quit(self) -> bool:
        """Write 'q' to ffmpeg's stdin. False (and logged) if the pipe is gone —
        on Windows a closed pipe raises EINVAL rather than EPIPE (probed)."""
        try:
            self.proc.stdin.write(b"q")
            self.proc.stdin.flush()
            self.proc.stdin.close()
            event(log, logging.INFO, "ffmpeg.quit_sent", pid=self.proc.pid)
            return True
        except (OSError, ValueError) as exc:   # ValueError: the pipe object is already closed
            event(log, logging.WARNING, "ffmpeg.quit_failed", pid=self.proc.pid, error=repr(exc))
            return False

    def wait(self, timeout_s: float):
        try:
            rc = self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return None
        self.join_reader()
        return rc

    def terminate(self, reason: str) -> int | None:
        event(log, logging.ERROR, "ffmpeg.terminate", pid=self.proc.pid, reason=reason)
        self.proc.kill()
        rc = self.wait(10)
        return rc

    def join_reader(self, timeout_s: float = 5) -> None:
        if self._reader is not None:
            self._reader.join(timeout_s)

    def close_pipes(self) -> None:
        """Release our ends of ffmpeg's pipes once it has exited."""
        if self.proc is None:
            return
        self.join_reader()
        for stream in (getattr(self.proc, "stdin", None), getattr(self.proc, "stderr", None)):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    def stderr_tail(self, n: int = 15) -> list[str]:
        with self._lock:
            return list(self.tail)[-n:]

    def media_time_s(self) -> float | None:
        """The last `time=` ffmpeg reported (its final stats line on exit)."""
        with self._lock:
            lines = list(self.tail)
        for line in reversed(lines):
            m = _TIME.search(line)
            if m:
                h, mnt, s = m.groups()
                return round(int(h) * 3600 + int(mnt) * 60 + float(s), 3)
        return None

    def capture_size(self) -> tuple[int, int] | None:
        with self._lock:
            lines = list(self.tail)
        for line in lines:
            m = _CAPTURE_SIZE.search(line)
            if m:
                return int(m.group(1)), int(m.group(2))
        return None


# ---------------------------------------------------------------------------
# 2. Request / result
# ---------------------------------------------------------------------------


@dataclass
class RecordRequest:
    slug: str | None = None
    collection: str | None = None
    flash: bool | None = None
    audio: bool | None = None
    pipeline: str | None = None
    scale: str | None = None
    fps: int | None = None
    # Session B: the agent reads the foreground window when the hotkey arrives
    # (A's Proposal 2) and says who started the recording ("terminal" | "agent").
    foreground: ForegroundInfo | None = None
    origin: str = "terminal"


@dataclass
class RecordResult:
    ok: bool
    message: str
    uid: str | None = None
    media_path: Path | None = None
    sidecar_path: Path | None = None
    exit_code: int | None = None
    stderr_tail: list[str] = field(default_factory=list)
    duration_s: float | None = None


class RecordError(RuntimeError):
    """A recording that could not start; the message is user-facing."""


# ---------------------------------------------------------------------------
# 3. Recorder
# ---------------------------------------------------------------------------


def _seconds_between(start_iso: str | None, end_iso: str | None) -> float | None:
    """end - start in seconds, both ISO strings with offsets; None if either is unusable."""
    try:
        return round((_dt.datetime.fromisoformat(end_iso) - _dt.datetime.fromisoformat(start_iso)).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


def resolve_ffmpeg(path: str, which: Callable[[str], str | None]) -> str | None:
    if os.path.isabs(path):
        return path if os.path.exists(path) else None
    return which(path)


class Recorder:
    def __init__(self, cfg: Config, control, *, popen: Callable = subprocess.Popen,
                 run: Callable = subprocess.run, flasher_factory: Callable | None = None,
                 foreground: Callable | None = None, which: Callable | None = None,
                 job_factory: Callable | None = None, today: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 status: Callable[[str], None] = print):
        from . import winapi
        import shutil
        self.cfg = cfg
        self.control = control
        self.popen = popen
        self.run = run
        self.flasher_factory = flasher_factory or self._default_flasher
        self.foreground = foreground or winapi.foreground_window
        self.which = which or shutil.which
        self.job_factory = job_factory or winapi.KillOnCloseJob
        self.today = today or _dt.date.today
        self.sleep = sleep
        self.status = status

    @staticmethod
    def _default_flasher():
        from .flash import TkFlasher
        return TkFlasher()

    # -- the flow ------------------------------------------------------------

    def record(self, req: RecordRequest, stop_event: threading.Event) -> RecordResult:
        cfg = self.cfg
        ffmpeg = resolve_ffmpeg(cfg.ffmpeg.path, self.which)
        if not ffmpeg:
            raise RecordError(f"ffmpeg not found ({cfg.ffmpeg.path!r}); run `peep doctor` "
                              f"(fix: winget install Gyan.FFmpeg)")
        collection = naming.validate_collection(req.collection or cfg.default_collection)
        fg = req.foreground if req.foreground is not None else self.foreground()
        slug = naming.slug_from_user(req.slug) if req.slug else naming.suggest_slug(fg.image, fg.title)
        coll_dir = cfg.root_path() / collection
        coll_dir.mkdir(parents=True, exist_ok=True)
        stem = naming.allocate_stem(str(coll_dir), self.today(), slug)
        capture = coll_dir / f"{stem}.recording.mkv"
        final = coll_dir / f"{stem}.{cfg.output.container}"
        side = coll_dir / f"{stem}.json"
        use_flash = cfg.flash.enabled if req.flash is None else req.flash
        use_audio = cfg.audio.enabled if req.audio is None else req.audio

        uid = catalog.new_uid()
        sc = catalog.new_sidecar(uid=uid, slug=slug, collection=collection, file=final.name,
                                 created=catalog.now_iso())
        sc["foreground"] = {"title": fg.title, "process": fg.process, "image": fg.image}
        sc["audio"] = ({"device": cfg.audio.device, "codec": "aac", "bitrate": cfg.audio.bitrate,
                        "offset_ms": cfg.audio.offset_ms} if use_audio else None)
        sc["flash"]["enabled"] = use_flash
        sc["timeline"]["origin"] = req.origin

        # Claim before writing anything: a refused claim (already recording) must leave no trace.
        self.control.claim({"uid": uid, "capture": str(capture), "final": str(final), "sidecar": str(side),
                            "origin": req.origin, "status": "starting"})
        proc: FfmpegProcess | None = None
        flasher = NullFlasher()
        try:
            catalog.write_sidecar(side, sc)   # reserves the stem while the capture runs
            event(log, logging.INFO, "record.begin", uid=uid, stem=stem, collection=collection,
                  slug_source="user" if req.slug else "foreground", fg_process=fg.process)
            if use_flash:
                flasher = self._prepare_flasher(sc)
            proc, pipeline, spec = self._start_capture(ffmpeg, capture, req, use_audio, sc)
            if proc is None:
                return self._finish_failed_start(sc, side, capture, uid, collection, spec)
            t0 = proc.started_mono
            timeline = sc["timeline"]
            timeline["ffmpeg_started_at"] = proc.started_at
            timeline["ffmpeg_ready_s"] = round(time.monotonic() - t0, 3)
            self.sleep(cfg.flash.lead_ms / 1000)
            if use_flash:
                rec = flasher.flash(cfg.flash.start_color, cfg.flash.duration_ms)
                sc["flash"]["start"] = rec.to_sidecar(t0)
            self.control.update_active(status="recording", recording_since=catalog.now_iso(),
                                       ffmpeg_started_at=proc.started_at)
            self.status(f"● recording → {final}\n  press q or Enter to stop (or run `peep stop` elsewhere)")

            def take_marks():
                self._take_marks(flasher if use_flash else None, sc, side, t0, proc.started_at)

            reason = self._wait_for_stop(proc, stop_event, on_tick=take_marks)
            if reason != "ffmpeg-exited":
                take_marks()              # a mark pressed just before the stop still counts
            self.control.update_active(status="stopping")
            timeline["stop_requested_at"] = catalog.now_iso()
            timeline["stop_requested_s"] = round(time.monotonic() - t0, 3)
            timeline["stop_reason"] = reason
            rc = proc.poll()
            if reason != "ffmpeg-exited":
                if use_flash:
                    rec = flasher.flash(cfg.flash.stop_color, cfg.flash.duration_ms)
                    sc["flash"]["stop"] = rec.to_sidecar(t0)
                self.sleep(cfg.flash.settle_ms / 1000)
                self.status("■ stopping…")
                proc.send_quit()
                rc = proc.wait(cfg.ffmpeg.stop_timeout_s)
                if rc is None:
                    rc = proc.terminate(f"no exit {cfg.ffmpeg.stop_timeout_s}s after q")
            else:
                proc.join_reader()
            timeline["ffmpeg_exited_at"] = catalog.now_iso()
            timeline["ffmpeg_exited_s"] = round(time.monotonic() - t0, 3)
            event(log, logging.INFO if rc == 0 else logging.ERROR, "ffmpeg.exit", pid=proc.proc.pid,
                  exit_code=rc, reason=reason)
            return self._finalize(sc, side, capture, final, proc, rc, ffmpeg, spec, uid, collection, reason)
        except BaseException as exc:
            # Anything unexpected (including Ctrl-C reaching this process): never leave a
            # sidecar claiming "recording". The capture file, if any, is left where it is.
            event(log, logging.ERROR, "record.aborted", uid=uid, error=repr(exc))
            if sc.get("status") == "recording":
                sc["status"] = "failed"
                sc["error"] = repr(exc)
                try:
                    catalog.write_sidecar(side, sc)
                except OSError as write_exc:
                    event(log, logging.ERROR, "sidecar.write_failed", path=str(side), error=repr(write_exc))
            raise
        finally:
            flasher.close()
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate("recorder exiting with ffmpeg still running")
                proc.close_pipes()
            self.control.release()

    # -- steps ---------------------------------------------------------------

    def _prepare_flasher(self, sc: dict):
        try:
            f = self.flasher_factory()
            f.prepare()
            return f
        except Exception as exc:  # no desktop / no tkinter: record anyway, but say so everywhere
            msg = f"clapper flash unavailable ({exc!r}); recording without it"
            event(log, logging.WARNING, "flash.unavailable", error=repr(exc))
            self.status(f"! {msg}")
            sc["flash"]["enabled"] = False
            sc["flash"]["error"] = repr(exc)
            return NullFlasher()

    def _start_capture(self, ffmpeg: str, capture: Path, req: RecordRequest, use_audio: bool, sc: dict):
        cfg = self.cfg
        primary = req.pipeline or cfg.video.pipeline
        order = [primary] + [p for p in cfg.video.fallback if p != primary]
        attempts = []
        spec = None
        for i, pipeline in enumerate(order):
            spec = ffmpeg_cmd.spec_from_config(cfg, ffmpeg=ffmpeg, output=str(capture), pipeline=pipeline,
                                               scale=req.scale, fps=req.fps, audio=use_audio)
            proc = FfmpegProcess(self.popen, job=self.job_factory())
            proc.start(ffmpeg_cmd.build_capture_argv(spec))
            state = proc.wait_ready(cfg.ffmpeg.startup_timeout_s)
            if state == "timeout":
                event(log, logging.WARNING, "ffmpeg.ready_timeout", pipeline=pipeline,
                      timeout_s=cfg.ffmpeg.startup_timeout_s)
                self.status(f"! ffmpeg has not reported its output after {cfg.ffmpeg.startup_timeout_s:g}s; "
                            f"continuing (check the log if the file is empty)")
                state = "ready"
            if state == "ready":
                sc["video"].update(pipeline=pipeline, encoder=ffmpeg_cmd.ENCODERS[pipeline], fps=spec.fps,
                                   output_idx=spec.output_idx)
                sc["ffmpeg"]["argv"] = proc.argv
                sc["ffmpeg"]["attempts"] = attempts
                return proc, pipeline, spec
            rc, tail = proc.poll(), proc.stderr_tail(12)
            proc.close_pipes()
            attempts.append({"pipeline": pipeline, "exit_code": rc, "stderr_tail": tail})
            event(log, logging.ERROR, "ffmpeg.start_failed", pipeline=pipeline, exit_code=rc, stderr_tail=tail)
            self._discard_empty(capture)
            nxt = order[i + 1] if i + 1 < len(order) else None
            self.status(f"! {pipeline} pipeline failed to start (ffmpeg exit {rc})"
                        + (f"; trying {nxt}" if nxt else ""))
            for line in tail[-4:]:
                self.status(f"    ffmpeg: {line}")
        sc["ffmpeg"]["attempts"] = attempts
        return None, None, spec

    def _wait_for_stop(self, proc: FfmpegProcess, stop_event: threading.Event, poll_s: float = 0.1,
                       on_tick: Callable[[], None] | None = None) -> str:
        while True:
            if stop_event.is_set():
                return getattr(stop_event, "reason", None) or "terminal"
            if self.control.stop_requested():
                return "peep-stop"
            if proc.poll() is not None:
                return "ffmpeg-exited"
            if on_tick is not None:
                on_tick()
            stop_event.wait(poll_s)

    def _take_marks(self, flasher, sc: dict, side: Path, t0: float, ffmpeg_started_at: str) -> None:
        """Turn pending mark requests into sidecar `marks` entries.

        `t` (A's reserved key) is the press, in seconds since the ffmpeg launch:
        the requester's wall-clock `requested_at` minus `ffmpeg_started_at`, so the
        <=100 ms poll delay does not shift it. When flashes are on, the mark-colour
        flash's own stamp (same clock as flash.start/stop) is kept beside it, and
        that flash is what session C finds in the video. The sidecar is rewritten
        straight away, so a crash later in the recording keeps its marks."""
        requests = self.control.take_marks()
        if not requests:
            return
        cfg = self.cfg
        for req in requests:
            consumed = time.monotonic()
            t = _seconds_between(ffmpeg_started_at, req.get("requested_at"))
            if t is None:
                t = round(consumed - t0, 3)
            mark = {"t": t, "label": str(req.get("label") or ""), "since_ffmpeg_start_s": t,
                    "at": req.get("requested_at"), "source": req.get("source"),
                    "consumed_s": round(consumed - t0, 3), "flash": None}
            if flasher is not None:
                mark["flash"] = flasher.flash(cfg.flash.mark_color, cfg.flash.duration_ms).to_sidecar(t0)
            sc["marks"].append(mark)
            n = len(sc["marks"])
            event(log, logging.INFO, "record.mark", n=n, t=t, source=mark["source"], label=mark["label"],
                  flashed=mark["flash"] is not None)
            self.status(f"◆ mark {n} at {t:.1f}s" + (f" ({mark['label']})" if mark["label"] else ""))
        try:
            catalog.write_sidecar(side, sc)
        except OSError as exc:      # the marks are still in memory and land with the final sidecar
            event(log, logging.WARNING, "sidecar.mark_write_failed", path=str(side), error=repr(exc))

    def _discard_empty(self, path: Path) -> None:
        try:
            if path.exists() and path.stat().st_size == 0:
                path.unlink()
                event(log, logging.INFO, "file.discard_empty", path=str(path))
        except OSError as exc:
            event(log, logging.WARNING, "file.discard_failed", path=str(path), error=repr(exc))

    def _finish_failed_start(self, sc, side, capture, uid, collection, spec) -> RecordResult:
        # Nothing was recorded: drop the stem-reserving sidecar rather than leave clutter in
        # the Videos folder. Every attempt's exit code and stderr tail is already in the log.
        sc["status"] = "failed"
        try:
            side.unlink()
        except FileNotFoundError:
            pass
        event(log, logging.ERROR, "record.start_failed", uid=uid, attempts=len(sc["ffmpeg"]["attempts"]))
        last = sc["ffmpeg"]["attempts"][-1] if sc["ffmpeg"]["attempts"] else {}
        return RecordResult(False, "every pipeline failed to start; see the ffmpeg lines above, "
                                   "`peep doctor`, and the log", uid=uid, sidecar_path=None,
                            exit_code=last.get("exit_code"), stderr_tail=last.get("stderr_tail", []))

    def _finalize(self, sc, side, capture, final, proc, rc, ffmpeg, spec, uid, collection, reason) -> RecordResult:
        cfg = self.cfg
        root = cfg.root_path()
        size = proc.capture_size()
        sc["video"]["capture_size"] = f"{size[0]}x{size[1]}" if size else None
        out = ffmpeg_cmd.output_size(spec, size)
        sc["video"]["output_size"] = f"{out[0]}x{out[1]}" if out else None
        media_t = proc.media_time_s()
        sc["duration_s"] = media_t if media_t is not None else sc["timeline"].get("ffmpeg_exited_s")
        sc["ffmpeg"]["exit_code"] = rc
        has_data = capture.exists() and capture.stat().st_size > 0
        if rc != 0 or not has_data:
            tail = proc.stderr_tail(15)
            sc["ffmpeg"]["stderr_tail"] = tail
            sc["status"] = "failed"
            media = None
            if has_data:   # keep what was captured, minus the in-progress marker; Matroska plays as-is
                media = capture.with_name(capture.name.replace(".recording.mkv", ".mkv"))
                os.rename(capture, media)
                sc["video"]["container"] = "mkv"
            sc["file"] = media.name if media else None
            catalog.write_sidecar(side, sc)
            catalog.Catalog(root).append({"event": "failed", "uid": uid, "collection": collection,
                                          "file": catalog.relpath_posix(root, media) if media else "",
                                          "title": sc["title"], "created": sc["created"],
                                          "duration_s": sc["duration_s"], "exit_code": rc, "reason": reason})
            why = "ffmpeg stopped on its own" if reason == "ffmpeg-exited" else f"ffmpeg exited {rc}"
            kept = f"; partial capture kept at {media}" if media else ""
            return RecordResult(False, f"recording failed: {why}{kept}", uid=uid, media_path=media,
                                sidecar_path=side, exit_code=rc, stderr_tail=tail, duration_s=sc["duration_s"])

        media = capture
        if cfg.output.container == "mp4":
            media = self._remux(ffmpeg, capture, final, sc)
        else:
            os.rename(capture, final)
            media = final
        sc["video"]["container"] = media.suffix.lstrip(".")
        sc["file"] = media.name
        sc["status"] = "ok"
        catalog.write_sidecar(side, sc)
        catalog.Catalog(root).append({"event": "recorded", "uid": uid, "collection": collection,
                                      "file": catalog.relpath_posix(root, media), "title": sc["title"],
                                      "created": sc["created"], "duration_s": sc["duration_s"]})
        dur = f"{sc['duration_s']:.1f}s" if isinstance(sc["duration_s"], (int, float)) else "?s"
        return RecordResult(True, f"saved {media} ({dur})", uid=uid, media_path=media, sidecar_path=side,
                            exit_code=rc, duration_s=sc["duration_s"])

    def _remux(self, ffmpeg: str, capture: Path, final: Path, sc: dict) -> Path:
        """Capture -> MP4. On failure the Matroska is kept (renamed to <stem>.mkv)
        and the reason is logged, printed and stored in the sidecar."""
        argv = ffmpeg_cmd.build_remux_argv(ffmpeg, str(capture), str(final))
        sc["ffmpeg"]["remux_argv"] = argv
        event(log, logging.INFO, "ffmpeg.argv", argv=argv, cmdline=subprocess.list2cmdline(argv), purpose="remux")
        try:
            res = self.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=600)
            rc, err = res.returncode, (res.stderr or b"").decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired) as exc:
            rc, err = None, repr(exc)
        event(log, logging.INFO if rc == 0 else logging.ERROR, "ffmpeg.remux_exit", exit_code=rc,
              stderr_tail=err.strip().splitlines()[-5:])
        if rc == 0 and final.exists() and final.stat().st_size > 0:
            capture.unlink()
            return final
        keep = capture.with_name(capture.name.replace(".recording.mkv", ".mkv"))
        os.rename(capture, keep)
        sc["remux_error"] = {"exit_code": rc, "stderr_tail": err.strip().splitlines()[-5:]}
        self.status(f"! MP4 remux failed (exit {rc}); kept the Matroska capture: {keep}")
        if final.exists() and final.stat().st_size == 0:
            final.unlink()
        return keep
