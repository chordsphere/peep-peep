"""Process control and the record flow.

`FfmpegProcess` owns one ffmpeg child: starts it (argv logged verbatim
first), drains its stderr into the log and a tail buffer, detects
readiness, stops it by writing `q` to its stdin (interop does not
forward Ctrl-C reliably; a Windows pipe does), and surfaces exit code
and stderr tail on failure.

`Recorder.record()` is the whole recording, start to catalog:

  name    foreground window -> slug (unless given) -> collision-free stem,
          free in the folder (case-insensitively) and in the catalog (C1a)
  claim   sidecar created exclusively at once (status "recording", reserves
          the stem), control file claimed so `peep stop` can find us
  segment one or more (session C1a: hard pause / resume), each:
    audio   (A.1) one WASAPI capture child per audio source, each serving raw
            PCM on a localhost port; a child that cannot start is reported
            loudly and the segment goes on without it
    start   ffmpeg launched into <stem>.recording.mkv (segment k >= 2:
            <stem>.seg<k>.recording.mkv); if the pipeline dies during
            start-up, the next one in video.fallback is tried, loudly. At
            ffmpeg's `Input #0` (its first ddagrab frame is VIDEO_EPOCH
            before that, measured), every child is sent that instant as its
            anchor: audio t=0 and video t=0 are the same moment
    flash   start flash once ffmpeg reports its output open (+ lead_ms), with
            the clap tone when system audio is recorded
    wait    until stop_event (q/Enter in the terminal, or the agent's hotkey), a
            stop-request file (`peep stop`), ffmpeg exiting on its own (a
            failure), or an accepted pause. Meanwhile requests (`peep mark|take|
            correct|pause`, the agent's chords) go through the event model
            (events.py) and show a corner patch (C1a) or the mark flash; each
            press's take state and feedback go to active.json for the pill (C1c).
            C1d: the agent's learn presses and its sampler's visual events
            (a learned screen appeared / went away) come the same way; they
            show no patch (one would draw over the screen being matched), and
            each learned screen's thumbnail is written as a small PNG under
            the state folder for the expanded pill (C1e)
    stop    stop flash, settle_ms, then `q`; escalate to terminate on timeout;
            then the audio children (they keep feeding ffmpeg until it exits,
            and ffmpeg's -rw_timeout bounds a hung one: no hang on the stop path)
    final   remux to <stem>.mp4 / <stem>.seg<k>.mp4 (streams copied) and drop
            the capture, or keep the Matroska
  pause   between segments nothing is captured; the sidecar and the catalog
          already hold the finished segments, so a crash while paused keeps
          them. Requests still count (placed at the boundary).
  final   sidecar completed (the event record, takes, summary); catalog event

Matroska is the capture container because a capture cut short by a
crash or power loss is still playable; MP4 is only produced from a
cleanly closed capture.

Sections:
  1. FfmpegProcess              (~line 85)
  2. Request / result           (~line 245)
  3. Recorder                   (~line 290)
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

from . import catalog, ffmpeg_cmd, naming, paths
from .config import Config, effective_sources, source_set
from . import screens
from .events import CORRECT, FORGET, LEARN, MARK, SCREEN, TAKE, TAKE_RULE, EventModel, Press, press_feedback
from .events import is_quiet as events_is_quiet
from .events import summary_line as events_summary_line
from .flash import NullFlasher, patch_corner
from .logsetup import event
from .winapi import ForegroundInfo

log = logging.getLogger("peep.recorder")

AUTO_TAKES_DIR = "auto-takes"          # C1d: <state>/auto-takes/<uid>.<ref>.png, one per learned screen

# When an audio child gets no anchor (ffmpeg printed no `Input #0` before connecting, which
# the probes never saw), it starts the stream this long before ffmpeg's connect instead:
# the median of connect - first video frame across the A.1 probe runs (424-442 ms).
FALLBACK_LEAD_MS = 430

# ---------------------------------------------------------------------------
# 1. FfmpegProcess
# ---------------------------------------------------------------------------

READY_MARKERS = ("Output #0",)          # printed once the muxer header is written: capture is live
_TIME = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_CAPTURE_SIZE = re.compile(r"Stream #0:0.*?Video:.*?(\d{3,5})x(\d{3,5})")
CREATE_NO_WINDOW = 0x08000000


class FfmpegProcess:
    def __init__(self, popen: Callable = subprocess.Popen, tail_lines: int = 200, job=None,
                 on_input0: Callable[[float], None] | None = None):
        self._popen_factory = popen
        self.proc = None
        self.argv: list[str] = []
        self.tail = collections.deque(maxlen=tail_lines)
        self.ready = threading.Event()
        self.started_mono: float | None = None   # time.monotonic() at launch: the t0 of the timeline
        self.started_at: str | None = None        # the same instant as local ISO time
        self._job = job
        # A.1: QPC stamps (time.perf_counter: QueryPerformanceCounter on Windows, the clock the
        # WASAPI children stamp audio with) of the launch and of ffmpeg's "Input #0" line.
        self.started_qpc: float | None = None
        self.input0_qpc: float | None = None
        self.on_input0 = on_input0
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, argv: list[str]) -> None:
        self.argv = list(argv)
        event(log, logging.INFO, "ffmpeg.argv", argv=self.argv, cmdline=subprocess.list2cmdline(self.argv))
        kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if sys.platform == "win32":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        self.started_mono = time.monotonic()
        self.started_qpc = time.perf_counter()
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
                if self.input0_qpc is None and line.startswith("Input #0"):
                    self._input0(time.perf_counter())
                with self._lock:
                    self.tail.append(line)
                log.debug("ffmpeg| %s", line)
                if not self.ready.is_set() and any(m in line for m in READY_MARKERS):
                    self.ready.set()
        except (OSError, ValueError) as exc:
            event(log, logging.WARNING, "ffmpeg.stderr_read_failed", error=repr(exc))

    def _input0(self, qpc: float) -> None:
        """ffmpeg has opened the screen input (it prints `Input #0` right after
        probing it); stamped on arrival, then handed to the recorder, which
        anchors the audio children before ffmpeg opens their inputs."""
        self.input0_qpc = qpc
        event(log, logging.INFO, "ffmpeg.input0", since_launch_ms=round((qpc - self.started_qpc) * 1000, 1))
        if self.on_input0 is not None:
            try:
                self.on_input0(qpc)
            except Exception as exc:     # never let an anchor problem kill the stderr drain
                event(log, logging.ERROR, "ffmpeg.input0_callback_failed", error=repr(exc))

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
    audio: bool | None = None            # A's switch: False = no audio at all (kept for callers that set it)
    audio_sources: str | None = None     # A.1: system | mic | both | none; None = config audio.sources
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
    duration_s: float | None = None          # the whole session: every segment's duration added up
    # Session C1a: what the dialog's status line needs, computed from the event record.
    segments: int = 1
    summary: dict | None = None              # events.summarize(): takes, kept_s, total_s, whole, kept
    size_bytes: int | None = None            # every segment's file together
    suffixed_from: str | None = None         # the stem that was wanted, when a counter was added


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


def audio_summary(block: dict | None) -> str:
    """One status line: what audio this recording has, and from which device."""
    if not block or not block.get("tracks"):
        return "audio: none" if not block else "audio: none (no source could start; see the warnings above)"
    parts = []
    for src in ("system", "mic"):
        b = block.get(src)
        if not b or b.get("error"):
            continue
        name = (b.get("endpoint") or {}).get("name") or b.get("device") or "?"
        parts.append(f"{src} ({name})")
    mix = " mixed" if block.get("mix") == "mix" and len(parts) > 1 else (" as separate tracks" if len(parts) > 1 else "")
    return "audio: " + " + ".join(parts) + mix


def _default_capture_factory(job):
    from .wasapi import CaptureProcess
    return CaptureProcess(job=job)


def _default_player(path: str) -> bool:
    from .wasapi import play_wav_async
    return play_wav_async(path)


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
                 status: Callable[[str], None] = print,
                 capture_factory: Callable | None = None, player: Callable[[str], bool] | None = None,
                 clap_path: Path | None = None):
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
        self.capture_factory = capture_factory or _default_capture_factory
        self.player = player or _default_player
        self.clap_path = clap_path

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
        root = cfg.root_path()
        coll_dir = root / collection
        coll_dir.mkdir(parents=True, exist_ok=True)
        today = self.today()
        stem = naming.allocate_stem(str(coll_dir), today, slug, reserved=self._catalog_stems(root, collection))
        wanted_stem = naming.stem_for(today, slug)
        capture = coll_dir / f"{stem}{naming.segment_suffix(1, 'capture')}"
        final = coll_dir / f"{stem}.{cfg.output.container}"
        side = coll_dir / f"{stem}.json"
        use_flash = cfg.flash.enabled if req.flash is None else req.flash
        sources = "none" if req.audio is False else effective_sources(cfg.audio, req.audio_sources)
        wanted = source_set(sources)

        uid = catalog.new_uid()
        sc = catalog.new_sidecar(uid=uid, slug=slug, collection=collection, file=final.name,
                                 created=catalog.now_iso())
        sc["foreground"] = {"title": fg.title, "process": fg.process, "image": fg.image}
        sc["audio"] = self._audio_block(sources) if wanted else None
        sc["flash"]["enabled"] = use_flash
        sc["timeline"]["origin"] = req.origin
        if stem != wanted_stem:
            sc["name_suffixed_from"] = wanted_stem

        # Claim before writing anything: a refused claim (already recording) must leave no trace.
        self.control.claim({"uid": uid, "capture": str(capture), "final": str(final), "sidecar": str(side),
                            "origin": req.origin, "status": "starting", "segment": 1, "captured_s": 0.0,
                            "takes": 0, "take_open": False})
        self._clear_thumbs()                 # a learned screen belongs to one recording only (C1d)
        sess = _Session(req=req, sc=sc, side=side, stem=stem, coll_dir=coll_dir, uid=uid, collection=collection,
                        use_flash=use_flash, sources=sources, wanted=wanted, ffmpeg=ffmpeg, final=final,
                        model=EventModel(cfg.agent.debounce_ms / 1000))
        try:
            try:
                catalog.create_json_exclusive(side, sc)    # reserves the stem; never overwrites a sidecar
            except FileExistsError as exc:
                raise RecordError(f"{side.name} appeared while this recording was being named; "
                                  f"nothing was overwritten, try again") from exc
            sess.own_side = True
            event(log, logging.INFO, "record.begin", uid=uid, stem=stem, collection=collection,
                  slug_source="user" if req.slug else "foreground", fg_process=fg.process,
                  **({"suffixed_from": wanted_stem} if stem != wanted_stem else {}))
            if use_flash:
                sess.flasher = self._prepare_flasher(sc)
            index = 1
            while True:
                outcome = self._run_segment(sess, index, stop_event)
                if outcome == "failed-start":
                    if index == 1:
                        return self._finish_failed_start(sc, side, capture, uid, collection, None)
                    self.status(f"! segment {index} could not start (see the ffmpeg lines above); the recording "
                                f"was saved with the {index - 1} segment(s) captured before the pause")
                    sess.reason = sess.reason or "resume-failed"
                    break
                if outcome != "pause":
                    break
                after = self._pause_wait(sess, index, stop_event)
                if after != "resume":
                    sess.reason = after
                    break
                index += 1
            return self._finalize_session(sess)
        except BaseException as exc:
            # Anything unexpected (including Ctrl-C reaching this process): never leave a
            # sidecar claiming "recording". The capture file, if any, is left where it is.
            event(log, logging.ERROR, "record.aborted", uid=uid, error=repr(exc))
            if sc.get("status") in ("recording", "paused") and sess.own_side:
                sc["status"] = "failed"
                sc["error"] = repr(exc)
                self._sync_events(sess)
                try:
                    catalog.write_sidecar(side, sc)
                except OSError as write_exc:
                    event(log, logging.ERROR, "sidecar.write_failed", path=str(side), error=repr(write_exc))
            raise
        finally:
            sess.flasher.close()
            self.control.release()
            self._clear_thumbs()

    def _catalog_stems(self, root: Path, collection: str) -> set[str]:
        """The catalog's stems in this collection (the naming audit reserves them
        even when their files are gone). An unreadable catalog is logged, and the
        folder listing alone decides."""
        try:
            return catalog.Catalog(root).stems_in(collection)
        except OSError as exc:
            event(log, logging.WARNING, "catalog.unreadable_for_naming", root=str(root), error=repr(exc))
            return set()

    # -- one segment ---------------------------------------------------------------

    def _segment_context(self, sess: "_Session", index: int) -> dict:
        """The blocks a segment fills (video, audio, flash, timeline, ffmpeg). Segment 1
        fills the sidecar's own top-level blocks, so a recording that is never paused
        has exactly A/A.1/B's sidecar; later segments get fresh blocks of the same shape."""
        if index == 1:
            return sess.sc
        return {"video": {k: None for k in sess.sc["video"]},
                "audio": self._audio_block(sess.sources) if sess.wanted else None,
                "flash": {"enabled": sess.sc["flash"]["enabled"], "start": None, "stop": None},
                "timeline": {}, "ffmpeg": {"argv": None, "exit_code": None, "stderr_tail": None, "remux_argv": None}}

    def _run_segment(self, sess: "_Session", index: int, stop_event: threading.Event) -> str:
        """Capture one segment, start to finalized file. Returns why it ended:
        "pause", "failed-start", or the stop reason (A's: terminal / peep-stop /
        hotkey... / ffmpeg-exited)."""
        cfg = self.cfg
        segsc = self._segment_context(sess, index)
        capture = sess.coll_dir / f"{sess.stem}{naming.segment_suffix(index, 'capture')}"
        block = {"index": index, "capture_file": capture.name, "file": None, "status": "recording"}
        sess.segments.append((block, segsc))
        proc: FfmpegProcess | None = None
        captures: dict = {}
        try:
            captures = self._start_audio(sess.wanted, segsc)
            proc, pipeline, spec = self._start_capture(sess.ffmpeg, capture, sess.req, sess.wanted, captures, segsc)
            if proc is None:
                block["status"] = "failed-start"
                self._stop_audio(captures, segsc)
                return "failed-start"
            t0 = proc.started_mono
            timeline = segsc["timeline"]
            timeline["ffmpeg_started_at"] = proc.started_at
            timeline["ffmpeg_ready_s"] = round(time.monotonic() - t0, 3)
            self.sleep(cfg.flash.lead_ms / 1000)
            if sess.use_flash:
                if "system" in captures and cfg.audio.clap:
                    self._clap(segsc, proc)
                rec = sess.flasher.flash(cfg.flash.start_color, cfg.flash.duration_ms)
                segsc["flash"]["start"] = rec.to_sidecar(t0)
            epoch = (proc.input0_qpc - cfg.audio.epoch_lead_ms / 1000) if proc.input0_qpc is not None \
                else proc.started_qpc
            sess.model.segment_started(index, epoch, proc.started_qpc)
            sess.model.set_paused(False)
            if index > 1:
                prev = sess.model.segments.get(index - 1) or {}
                sess.model.note_pause_capture(index - 1, prev.get("end_qpc"), epoch)
            sess.live = _Live(index=index, segsc=segsc, t0=t0, started_at=proc.started_at, proc=proc)
            block.update(started_at=proc.started_at, ffmpeg_started_qpc=proc.started_qpc,
                         input0_qpc=proc.input0_qpc, video_epoch_qpc_est=round(epoch, 6))
            self.control.update_active(status="recording", recording_since=catalog.now_iso(),
                                       ffmpeg_started_at=proc.started_at, segment=index,
                                       captured_s=round(sess.captured_s, 3), paused_since=None,
                                       **self._take_counts(sess))
            if index == 1:
                note = (f"\n  ({sess.sc['name_suffixed_from']} was taken, so this one is {sess.stem})"
                        if sess.sc.get("name_suffixed_from") else "")
                self.status(f"● recording → {sess.final}{note}\n  {audio_summary(segsc.get('audio'))}\n"
                            f"  press q or Enter to stop (or run `peep stop` elsewhere)")
            else:
                self.status(f"● recording again: segment {index} → "
                            f"{sess.coll_dir / (sess.stem + naming.segment_suffix(index, 'mp4'))}")
            # Requests that arrived during the pause or the resume are placed by their own stamps.
            pending, sess.pending = sess.pending, []
            reason = self._handle_requests(sess, pending) if pending else None
            if reason is None:
                reason = self._wait_for_stop(proc, stop_event, on_tick=lambda: self._sweep(sess))
            if reason != "ffmpeg-exited":
                self._sweep(sess)          # a press just before the stop still counts
                if reason != "pause" and sess.pause_requested:
                    sess.pause_requested = False      # a stop wins over a pause pressed at the same moment
                    sess.model.cancel_pending_pause("superseded-by-stop")
            pausing = reason == "pause"
            self.control.update_active(status="pausing" if pausing else "stopping")
            timeline["stop_requested_at"] = catalog.now_iso()
            timeline["stop_requested_s"] = round(time.monotonic() - t0, 3)
            timeline["stop_reason"] = reason
            rc = proc.poll()
            stopped_qpc = None
            if reason != "ffmpeg-exited":
                if sess.use_flash:
                    rec = sess.flasher.flash(cfg.flash.stop_color, cfg.flash.duration_ms)
                    segsc["flash"]["stop"] = rec.to_sidecar(t0)
                self.sleep(cfg.flash.settle_ms / 1000)
                self.status("❚❚ pausing…" if pausing else "■ stopping…")
                stopped_qpc = time.perf_counter()
                proc.send_quit()
                rc = proc.wait(cfg.ffmpeg.stop_timeout_s)
                if rc is None:
                    rc = proc.terminate(f"no exit {cfg.ffmpeg.stop_timeout_s}s after q")
            else:
                proc.join_reader()
                stopped_qpc = time.perf_counter()
            sess.live = None
            timeline["ffmpeg_exited_at"] = catalog.now_iso()
            timeline["ffmpeg_exited_s"] = round(time.monotonic() - t0, 3)
            event(log, logging.INFO if rc == 0 else logging.ERROR, "ffmpeg.exit", pid=proc.proc.pid,
                  exit_code=rc, reason=reason, segment=index)
            self._stop_audio(captures, segsc)
            captures = {}
            self._finalize_segment(sess, block, segsc, capture, proc, rc, spec)
            sess.model.segment_ended(index, stopped_qpc, block.get("duration_s"))
            sess.captured_s += block.get("duration_s") or 0.0
            if pausing and block["status"] == "ok":
                return "pause"
            if pausing:
                self.status(f"! segment {index} did not finish cleanly; ending the recording instead of pausing")
                sess.reason = "ffmpeg-exited"
                sess.model.cancel_pending_pause("segment-failed")
                return "ffmpeg-exited"
            sess.reason = reason
            return reason
        finally:
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate("recorder exiting with ffmpeg still running")
                proc.close_pipes()
            for cap in captures.values():      # no-op when _stop_audio already ran
                if cap.poll() is None:
                    cap.stop(2.0)

    # -- pause ---------------------------------------------------------------------

    def _pause_wait(self, sess: "_Session", index: int, stop_event: threading.Event, poll_s: float = 0.1) -> str:
        """Hard pause: nothing is captured. The finished segments are already on disk,
        and are catalogued now, so a crash or a power cut while paused keeps them.
        Returns "resume", or the stop reason (q / `peep stop` / a hotkey stop)."""
        sess.model.set_paused(True)
        sess.pause_requested = False
        sess.sc["status"] = "paused"
        sess.sc["duration_s"] = round(sess.captured_s, 3)
        self._persist(sess)
        try:
            self._catalog_session(sess, in_progress=True)
        except OSError as exc:      # the sidecar still lists the segments; the final event comes at the stop
            event(log, logging.ERROR, "catalog.pause_append_failed", error=repr(exc))
            self.status(f"! could not catalogue the paused recording yet ({exc}); it will be at the stop")
        self.control.update_active(status="paused", paused_since=catalog.now_iso(), segment=index,
                                   captured_s=round(sess.captured_s, 3), **self._take_counts(sess))
        event(log, logging.INFO, "record.paused", uid=sess.uid, after_segment=index,
              captured_s=round(sess.captured_s, 3))
        self.status(f"❚❚ paused after segment {index} ({fmt_s(sess.captured_s)} captured so far, saved). "
                    f"Resume with the pause hotkey or `peep resume`; q or `peep stop` ends the recording.")
        pending, sess.pending = sess.pending, []
        while True:
            stop = (getattr(stop_event, "reason", None) or "terminal") if stop_event.is_set() else \
                ("peep-stop" if self.control.stop_requested() else None)
            if stop is not None:
                # presses that came with the stop still count (placed after the last segment)
                sess.pending = pending + [Press.from_request(r) for r in self.control.take_requests()]
                return stop
            requests = pending + [Press.from_request(r) for r in self.control.take_requests()]
            pending = []
            for i, press in enumerate(requests):
                rec = self._handle_press(sess, press)
                if rec.get("action") == "resume":
                    sess.pending = requests[i + 1:]
                    sess.sc["status"] = "recording"
                    event(log, logging.INFO, "record.resume", uid=sess.uid, next_segment=index + 1)
                    self.control.update_active(status="resuming")
                    self.status("● resuming…")
                    return "resume"
            stop_event.wait(poll_s)

    # -- requests: take / retake / pause / resume / mark ------------------------------

    def _sweep(self, sess: "_Session") -> str | None:
        """One poll's worth of control-file requests (A's 100 ms tick); "pause" once a
        pause was accepted, which ends the segment."""
        reqs = self.control.take_requests()
        if not reqs:
            return "pause" if sess.pause_requested else None
        return self._handle_requests(sess, [Press.from_request(r) for r in reqs])

    def _handle_requests(self, sess: "_Session", presses: list) -> str | None:
        for i, press in enumerate(presses):
            if sess.pause_requested:          # everything after an accepted pause waits for the pause
                sess.pending.extend(presses[i:])
                break
            self._handle_press(sess, press)
        return "pause" if sess.pause_requested else None

    def _handle_press(self, sess: "_Session", press: Press) -> dict:
        """Decide one press in the event model, show its fiducial while capturing,
        tell the user, keep the sidecar and active.json current (the take state
        and this press's feedback, for the pill: C1c)."""
        cfg = self.cfg
        rec = sess.model.press(press, time.perf_counter())
        live = sess.live
        if rec["kind"] in (LEARN, FORGET, SCREEN):
            self._handle_auto(sess, press, rec)
            return rec
        if not rec["accepted"]:
            event(log, logging.INFO, "record.event_ignored", kind=rec["kind"], why=rec["ignored"],
                  source=press.source, **(rec.get("debounce") or {}))
            self.status(f"· {rec['kind']} ignored ({rec['ignored']})")
            self._persist(sess)
            self._publish_press(sess, rec)
            return rec
        kind, action = rec["kind"], rec["action"]
        if action == "pause":
            sess.pause_requested = True
            event(log, logging.INFO, "record.pause_requested", segment=rec.get("segment"), source=press.source)
            self._publish_press(sess, rec)
            return rec
        if action == "resume":
            self._publish_press(sess, rec)
            return rec
        color = {"open": cfg.flash.take_open_color, "close": cfg.flash.take_close_color}.get(action)
        if kind == CORRECT:
            color = cfg.flash.correct_color
        elif kind == MARK:
            color = cfg.flash.mark_color
        if live is not None and sess.use_flash:
            full = kind == MARK and cfg.flash.mark_style == "full"
            fid = self._fiducial(sess, color, full)
            # `segment`: where the patch was shown. Usually the press's own segment, but a press made
            # while resuming is placed in the pause and shows its patch in this one (the render needs it).
            rec["fiducial"] = {**fid.to_sidecar(live.t0), "kind": f"{kind}-{action}" if kind in (TAKE, CORRECT)
                               else kind, "segment": live.index}
        if kind == MARK:
            self._add_mark(sess, press, rec)
        else:
            corr = rec.get("correction") or {}
            what = {("take", "open"): f"▶ take {rec['take']}", ("take", "close"): f"■ take {rec['take']} closed",
                    ("correct", "moved-close"): f"⌫ correct: take {rec['take']}'s close moved here "
                                                f"({fmt_signed_s(corr.get('moved_s'))})",
                    ("correct", "dropped-take"): f"⌫ correct: take {rec['discarded_take']} dropped, "
                                                 f"take {rec['take']} open"}
            line = what.get((kind, action)) or f"{kind} {action}"
            where = "" if rec.get("segment") else f" (paused: at the boundary after segment {rec.get('after_segment')})"
            self.status(line + where)
            event(log, logging.INFO, "record.event", kind=kind, action=action, take=rec["take"],
                  discarded=rec["discarded_take"], moved_s=corr.get("moved_s"), segment=rec.get("segment"),
                  media_s=rec.get("media_s"), source=press.source, fiducial=bool(rec["fiducial"]))
        self._persist(sess)
        self._publish_press(sess, rec)
        return rec

    def _handle_auto(self, sess: "_Session", press: Press, rec: dict) -> None:
        """C1d: a learn, a forget or a visual event. No fiducial is ever shown for
        these (a patch would draw over the very screen being matched; the learn
        press is recorded instead). A learned screen's thumbnail goes to a PNG
        under the state folder (C1e's expanded pill shows it) and, as hex, into
        the sidecar's auto_takes block."""
        kind, screen = rec["kind"], rec.get("screen")
        eff = rec.get("screen_effect") or {}
        if kind == LEARN and rec["accepted"]:
            ref = sess.model.references.get(screen) or {}
            sess.thumb_files[ref.get("ref")] = self._write_thumb(sess, ref)
        if rec["accepted"]:
            line = {"learned": f"⇥ {screen} screen learned", "relearned": f"⇥ {screen} screen learned again",
                    "forgotten": f"⇥ {screen} screen forgotten", "close": f"◇ {screen} screen: take {rec['take']} closed",
                    "open": f"◇ {screen} screen gone: take {rec['take']} opened",
                    "pending": f"◇ {screen} screen: a take opens when it goes",
                    "gone": None}.get(rec.get("action"))
            if kind == LEARN and eff.get("action") in ("close", "pending"):
                line += {"close": f": take {eff.get('take')} closed at its first frame",
                         "pending": ": a take opens when it goes"}[eff["action"]]
            elif kind == LEARN and eff.get("action") == "ignored":
                line += f" ({eff.get('reason')})"
            if line:
                self.status(line)
        else:
            self.status(f"· {screen or '?'} screen {kind} ignored ({rec['ignored']})")
        event(log, logging.INFO, "record.auto_take", kind=kind, screen=screen, change=rec.get("change"),
              action=rec.get("action"), ignored=rec.get("ignored"), effect=eff.get("action"), take=rec.get("take"),
              segment=rec.get("segment"), media_s=rec.get("media_s"), ref=rec.get("ref"), score=rec.get("score"))
        self._persist(sess)
        if events_is_quiet(rec):
            self._publish_state(sess)                # nothing anyone needs telling: no new feedback line
        else:
            self._publish_press(sess, rec)

    def _thumbs_dir(self) -> Path:
        return Path(self.control.dir) / AUTO_TAKES_DIR

    def _write_thumb(self, sess: "_Session", ref: dict) -> str | None:
        """The learned screen's thumbnail as an 8-bit greyscale PNG; its path, or None
        (logged) when it cannot be written: the sidecar's hex still has it."""
        try:
            w, h = ref["size"]
            data = screens.png_grey(w, h, screens.from_hex(ref["thumb"], w, h))
            folder = self._thumbs_dir()
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{sess.uid}.{ref['ref']}.png"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            return str(path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            event(log, logging.WARNING, "record.thumb_write_failed", ref=ref.get("ref"), error=repr(exc))
            return None

    def _clear_thumbs(self) -> None:
        """Learned screens are this recording's only: their PNGs go at its start and
        its end (the sidecar keeps every thumbnail as hex)."""
        folder = self._thumbs_dir()
        if not folder.is_dir():
            return
        for p in folder.glob("*.png*"):
            try:
                p.unlink()
            except OSError as exc:
                event(log, logging.WARNING, "record.thumb_clear_failed", path=str(p), error=repr(exc))

    def _publish_state(self, sess: "_Session") -> None:
        try:
            self.control.update_active(**self._take_counts(sess))
        except OSError as exc:
            event(log, logging.WARNING, "control.update_failed", error=repr(exc))

    def _publish_press(self, sess: "_Session", rec: dict) -> None:
        """active.json after a press: the take counts, the take state the pill's
        hint is computed from, and this press's feedback (a new `seq` each time,
        so the pill shows it once, for its 1.5 s)."""
        sess.feedback_seq += 1
        mark_n = len(sess.sc["marks"]) if rec.get("action") == "mark" else None
        fb = press_feedback(rec, sess.feedback_seq, mark=mark_n)
        fb["at"] = catalog.now_iso()            # an agent started later never replays an old press as new
        try:
            self.control.update_active(**self._take_counts(sess), feedback=fb)
        except OSError as exc:
            event(log, logging.WARNING, "control.update_failed", error=repr(exc))

    def _fiducial(self, sess: "_Session", color: str, full: bool):
        """A corner patch (or, for a full-style mark, B's full-screen flash). A flasher
        without patch support (an older stand-in) falls back to the full flash, loudly."""
        f = self.cfg.flash
        patch = getattr(sess.flasher, "patch", None)
        if full or patch is None:
            if not full:
                event(log, logging.WARNING, "flash.patch_unsupported", flasher=type(sess.flasher).__name__)
            return sess.flasher.flash(color, f.duration_ms)
        corner = patch_corner(f.patch_corner, self.cfg.agent.pill_position)
        return patch(color, f.duration_ms, corner, f.patch_size_px, f.patch_margin_px)

    def _add_mark(self, sess: "_Session", press: Press, rec: dict) -> None:
        """B's `marks` entry, plus where the mark sits on the segment timeline.
        `t` (A's reserved key) is the press in seconds since that segment's ffmpeg
        launch, from the requester's wall clock, so the poll delay does not shift it."""
        live = sess.live
        consumed = time.monotonic()
        t = None
        if live is not None:
            t = _seconds_between(live.started_at, press.at)
            if t is None:
                t = round(consumed - live.t0, 3)
        mark = {"t": t, "label": press.label, "since_ffmpeg_start_s": t, "at": press.at, "source": press.source,
                "consumed_s": round(consumed - live.t0, 3) if live is not None else None, "flash": rec["fiducial"],
                "segment": rec.get("segment"), "media_s": rec.get("media_s"), "event": rec["id"]}
        if rec.get("segment") is None:
            mark["after_segment"] = rec.get("after_segment")
        sess.sc["marks"].append(mark)
        n = len(sess.sc["marks"])
        event(log, logging.INFO, "record.mark", n=n, t=t, source=press.source, label=press.label,
              flashed=rec["fiducial"] is not None, segment=rec.get("segment"))
        at = f"{t:.1f}s" if isinstance(t, (int, float)) else f"the boundary after segment {rec.get('after_segment')}"
        self.status(f"◆ mark {n} at {at}" + (f" ({press.label})" if press.label else ""))

    def _take_counts(self, sess: "_Session") -> dict:
        """For active.json: C1a's counts (`peep take` prints them) and C1c's take
        state as of now (the pill's two lines are computed from it)."""
        c = sess.model.counts()
        state = sess.model.take_state(time.perf_counter())
        state["at"] = catalog.now_iso()        # the pill grows an open take's time from here, on its own clock
        return {"takes": c["takes"], "take_open": c["open"], "take_state": state,
                "auto_takes": self._auto_takes_block(sess)}

    def _auto_takes_block(self, sess: "_Session") -> dict:
        """active.json's `auto_takes` (C1d, for C1e's expanded pill; README documents
        it): per screen whether it is learned and when, its thumbnail PNG, how often
        it was seen and when last, and the last thing it did to the takes."""
        block = sess.model.auto_takes_state()
        for k in ("start", "end"):
            ref = sess.model.references.get(k) or {}
            block[k]["thumb"] = sess.thumb_files.get(ref.get("ref")) if ref else None
            block[k]["size"] = ref.get("size")
            block[k]["thresholds"] = ref.get("thresholds")
        block["at"] = catalog.now_iso()
        return block

    def _sync_events(self, sess: "_Session") -> None:
        sc, m = sess.sc, sess.model
        sc["segments"] = [self._segment_block(block, segsc) for block, segsc in sess.segments]
        sc["events"], sc["takes"], sc["pauses"] = m.events, m.takes, m.pauses
        sc["take_rule"] = TAKE_RULE
        auto = m.auto_takes_record()
        if auto is not None:                  # C1d: only once a screen was learned (else C1c's sidecar, unchanged)
            sc["auto_takes"] = auto

    def _persist(self, sess: "_Session") -> None:
        """Rewrite the sidecar now, so a crash later keeps every event so far."""
        self._sync_events(sess)
        try:
            catalog.write_sidecar(sess.side, sess.sc)
        except OSError as exc:      # still in memory; it lands with the final sidecar
            event(log, logging.WARNING, "sidecar.event_write_failed", path=str(sess.side), error=repr(exc))

    def _wait_for_stop(self, proc: FfmpegProcess, stop_event: threading.Event, poll_s: float = 0.1,
                       on_tick: Callable[[], str | None] | None = None) -> str:
        while True:
            if stop_event.is_set():
                return getattr(stop_event, "reason", None) or "terminal"
            if self.control.stop_requested():
                return "peep-stop"
            if proc.poll() is not None:
                return "ffmpeg-exited"
            if on_tick is not None and on_tick() == "pause":
                return "pause"
            stop_event.wait(poll_s)

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

    # -- audio (A.1) -------------------------------------------------------------

    def _audio_block(self, sources: str) -> dict:
        """The sidecar's audio block. A's four keys keep their meaning (`device`
        is the microphone, None without one); the rest is additive, so
        peep.sidecar/1 readers keep working."""
        a = self.cfg.audio
        wanted = source_set(sources)
        return {"device": (a.device or None) if "mic" in wanted else None, "codec": "aac", "bitrate": a.bitrate,
                "offset_ms": a.offset_ms, "sources": sources, "mix": a.mix if len(wanted) > 1 else None,
                "mic_backend": a.mic_backend if "mic" in wanted else None, "tracks": [], "epoch": None,
                "clap": None, **{src: None for src in wanted}}

    def _start_audio(self, wanted: tuple, sc: dict) -> dict:
        """Start one WASAPI child per source that needs one. A child that fails
        is reported (status line, log, sidecar) and left out; the recording
        goes on, like A's flash-unavailable path."""
        cfg = self.cfg
        captures = {}
        for src in wanted:
            if src == "mic" and cfg.audio.mic_backend == "dshow":
                sc["audio"]["mic"] = {"backend": "dshow", "device": cfg.audio.device or ffmpeg_cmd.DEFAULT_MIC,
                                      "shift_ms": cfg.audio.dshow_align_ms + cfg.audio.offset_ms}
                continue
            selector = cfg.audio.system_device if src == "system" else cfg.audio.device
            cap = self.capture_factory(self.job_factory())
            try:
                ready = cap.start(src, selector, ready_timeout_s=cfg.ffmpeg.startup_timeout_s,
                                  fallback_lead_ms=FALLBACK_LEAD_MS)
            except Exception as exc:
                tail = cap.stderr_tail(6) if hasattr(cap, "stderr_tail") else []
                sc["audio"][src] = {"backend": "wasapi", "error": str(exc), "exit_code": getattr(cap, "exit_code", None),
                                    "stderr_tail": tail}
                event(log, logging.ERROR, "audio.source_failed", source=src, error=str(exc), stderr_tail=tail)
                self.status(f"! {src} audio unavailable ({exc}); recording without it — see `peep doctor`")
                continue
            captures[src] = cap
            sc["audio"][src] = {"backend": "wasapi", "endpoint": ready.endpoint, "format": ready.format.to_dict(),
                                "selector": selector or "default", "error": None}
            if src == "mic":
                sc["audio"]["device"] = ready.endpoint.get("name") or sc["audio"]["device"]
            event(log, logging.INFO, "audio.source_ready", source=src, port=ready.port,
                  endpoint=ready.endpoint.get("name"), format=ready.format.describe())
        return captures

    def _anchor(self, captures: dict, sc: dict, proc: "FfmpegProcess", input0_qpc: float) -> None:
        """Called from ffmpeg's stderr drain the moment `Input #0` arrives."""
        a = self.cfg.audio
        anchor = input0_qpc - a.epoch_lead_ms / 1000 - a.offset_ms / 1000
        for src, cap in captures.items():
            cap.send_anchor(anchor)
        if sc.get("audio") is not None:
            sc["audio"]["epoch"] = {"ffmpeg_started_qpc": proc.started_qpc, "input0_qpc": input0_qpc,
                                    "epoch_lead_ms": a.epoch_lead_ms, "offset_ms": a.offset_ms,
                                    "anchor_qpc": anchor, "video_epoch_qpc_est": input0_qpc - a.epoch_lead_ms / 1000}

    def _clap(self, sc: dict, proc: "FfmpegProcess") -> None:
        """The audible fiducial: a short tone through the default output at the
        start flash. The system track carries it exactly (loopback); C finds
        it near flash.start. The probe measured 45-110 ms from this call to the
        tone reaching the FxSound loopback."""
        path = Path(self.clap_path) if self.clap_path else paths.data_dir() / "clap.wav"
        try:
            if not path.exists():
                from .wasapi import tone_wav
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(tone_wav(1000.0, 120, amplitude=0.4))
            qpc = time.perf_counter()
            ok = self.player(str(path))
        except OSError as exc:
            ok, qpc = False, None
            event(log, logging.WARNING, "audio.clap_failed", error=repr(exc))
        sc["audio"]["clap"] = {"played": bool(ok), "requested_qpc": qpc, "freq_hz": 1000, "ms": 120,
                               "since_ffmpeg_start_s": round(qpc - proc.started_qpc, 3) if (ok and qpc) else None}

    def _stop_audio(self, captures: dict, sc: dict) -> None:
        """After ffmpeg has exited: stop each child (bounded wait, then kill)
        and keep what it reported. A child that died during the recording is
        a warning on the status line, never silence."""
        for src, cap in captures.items():
            event(log, logging.INFO, "audio.stop", source=src)
            rc = cap.stop(3.0)
            block = sc["audio"][src]
            block["connections"] = [{k: v for k, v in e.items() if k != "event"} for e in cap.all("connected")]
            stats = cap.last("stats") or {}
            block["stats"] = stats.get("timeline")
            block["first_audio_qpc"] = stats.get("first_audio_qpc")
            block["exit_code"] = rc
            if rc != 0 or stats.get("capture_error"):
                block["error"] = stats.get("capture_error") or f"exited {rc}"
                block["stderr_tail"] = cap.stderr_tail(10)
                self.status(f"! {src} audio capture ended badly ({block['error']}); "
                            f"the {src} track may be short — see the log")
            if src == "system" and block["connections"]:
                sc["audio"]["first_sample_qpc"] = block["connections"][-1].get("first_sample_qpc")

    # -- video ---------------------------------------------------------------------

    def _start_capture(self, ffmpeg: str, capture: Path, req: RecordRequest, wanted: tuple, captures: dict,
                       sc: dict):
        cfg = self.cfg
        primary = req.pipeline or cfg.video.pipeline
        order = [primary] + [p for p in cfg.video.fallback if p != primary]
        attempts = []
        spec = None
        ready = {src: cap.ready_info for src, cap in captures.items()}
        inputs = ffmpeg_cmd.audio_inputs_for(cfg, wanted, ready)
        if sc.get("audio") is not None:
            sc["audio"]["tracks"] = ffmpeg_cmd.audio_track_layout(inputs, cfg.audio.mix)
            if not inputs:
                self.status("! no audio source could start; recording video only")
        for i, pipeline in enumerate(order):
            spec = ffmpeg_cmd.spec_from_config(cfg, ffmpeg=ffmpeg, output=str(capture), pipeline=pipeline,
                                               scale=req.scale, fps=req.fps, audio_inputs=inputs)
            proc = FfmpegProcess(self.popen, job=self.job_factory())
            if captures:
                proc.on_input0 = (lambda p: lambda qpc: self._anchor(captures, sc, p, qpc))(proc)
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
                sc["timeline"]["ffmpeg_started_qpc"] = proc.started_qpc
                sc["timeline"]["input0_qpc"] = proc.input0_qpc
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

    def _finalize_segment(self, sess: "_Session", block: dict, segsc: dict, capture: Path, proc, rc, spec) -> None:
        """One segment's file: remux the clean capture to MP4 (A #3, now per
        segment), or keep the Matroska when ffmpeg failed or the remux did."""
        cfg = self.cfg
        size = proc.capture_size()
        segsc["video"]["capture_size"] = f"{size[0]}x{size[1]}" if size else None
        out = ffmpeg_cmd.output_size(spec, size)
        segsc["video"]["output_size"] = f"{out[0]}x{out[1]}" if out else None
        media_t = proc.media_time_s()
        block["duration_s"] = media_t if media_t is not None else segsc["timeline"].get("ffmpeg_exited_s")
        segsc["ffmpeg"]["exit_code"] = rc
        block["exit_code"] = rc
        has_data = capture.exists() and capture.stat().st_size > 0
        if rc != 0 or not has_data:
            segsc["ffmpeg"]["stderr_tail"] = proc.stderr_tail(15)
            block["status"] = "failed"
            media = None
            if has_data:   # keep what was captured, minus the in-progress marker; Matroska plays as-is
                media = capture.with_name(capture.name.replace(".recording.mkv", ".mkv"))
                catalog.move_no_clobber(capture, media)
                segsc["video"]["container"] = "mkv"
            block["file"] = media.name if media else None
            return
        index = block["index"]
        if cfg.output.container == "mp4":
            final = capture.with_name(sess.stem + naming.segment_suffix(index, "mp4"))
            media = self._remux(sess.ffmpeg, capture, final, segsc)
        else:
            media = capture.with_name(sess.stem + naming.segment_suffix(index, "mkv"))
            catalog.move_no_clobber(capture, media)
        segsc["video"]["container"] = media.suffix.lstrip(".")
        block["file"] = media.name
        block["status"] = "ok"

    def _segment_block(self, block: dict, segsc: dict) -> dict:
        """The sidecar's `segments[i]`: identity, file, status and timing, plus that
        segment's own flash / timeline / video / audio / ffmpeg blocks."""
        out = dict(block)
        out["stop_reason"] = segsc["timeline"].get("stop_reason")
        for key in ("flash", "timeline", "video", "audio", "ffmpeg"):
            out[key] = segsc.get(key)
        if segsc.get("remux_error"):
            out["remux_error"] = segsc["remux_error"]
        return out

    def _session_size(self, sess: "_Session") -> int | None:
        total = 0
        for block, _ in sess.segments:
            if block.get("file"):
                try:
                    total += (sess.coll_dir / block["file"]).stat().st_size
                except OSError:
                    return None
        return total

    def _catalog_session(self, sess: "_Session", in_progress: bool = False) -> None:
        """Append the session's catalog event: `recorded` when any segment holds
        footage, else `failed`. Appended at each pause too (in_progress), so the
        segments finished so far survive a crash while paused; the fold keeps the
        last event per uid, so the final one simply replaces it."""
        sc, root = sess.sc, self.cfg.root_path()
        files = [b for b, _ in sess.segments if b.get("file")]
        media = sess.coll_dir / files[0]["file"] if files else None
        ok = any(b.get("status") == "ok" for b, _ in sess.segments)
        rec = {"event": "recorded" if ok else "failed", "uid": sess.uid, "collection": sess.collection,
               "file": catalog.relpath_posix(root, media) if media else "", "title": sc["title"],
               "created": sc["created"], "duration_s": sc["duration_s"]}
        if len(sess.segments) > 1 or in_progress:
            rec["segments"] = len([b for b, _ in sess.segments if b.get("file")])
        if in_progress:
            rec["in_progress"] = "paused"
        if not ok:
            rec.update(exit_code=sc["ffmpeg"]["exit_code"], reason=sess.reason)
        catalog.Catalog(root).append(rec)

    def _finalize_session(self, sess: "_Session") -> RecordResult:
        """Close the event record, complete the sidecar, catalogue the session."""
        sc = sess.sc
        blocks = [b for b, _ in sess.segments if b.get("status") != "failed-start"]
        sess.segments = [(b, s) for b, s in sess.segments if b.get("status") != "failed-start"]
        durations = [b.get("duration_s") for b in blocks]
        sc["duration_s"] = round(sum(d for d in durations if isinstance(d, (int, float))), 3) if blocks else None
        held, sess.pending = sess.pending, []
        for press in held:                 # held over a pause the session never resumed from: still recorded
            self._handle_press(sess, press)
        sess.model.cancel_pending_pause("recording-ended")
        summary = sess.model.finish()
        sc["summary"] = summary
        self._sync_events(sess)
        ok_blocks = [b for b in blocks if b.get("status") == "ok"]
        first_file = next((b["file"] for b in blocks if b.get("file")), None)
        sc["file"] = first_file
        media = sess.coll_dir / first_file if first_file else None
        reason = sess.reason
        if not ok_blocks:
            sc["status"] = "failed"
            catalog.write_sidecar(sess.side, sc)
            self._catalog_session(sess)
            seg1 = sess.segments[0][1] if sess.segments else sc
            tail = (seg1.get("ffmpeg") or {}).get("stderr_tail") or []
            rc = (seg1.get("ffmpeg") or {}).get("exit_code")
            why = "ffmpeg stopped on its own" if reason == "ffmpeg-exited" else f"ffmpeg exited {rc}"
            kept = f"; partial capture kept at {media}" if media else ""
            return RecordResult(False, f"recording failed: {why}{kept}", uid=sess.uid, media_path=media,
                                sidecar_path=sess.side, exit_code=rc, stderr_tail=tail, duration_s=sc["duration_s"],
                                segments=len(blocks), summary=summary)
        sc["status"] = "ok"
        if len(ok_blocks) < len(blocks):
            sc["segment_failures"] = len(blocks) - len(ok_blocks)
            for b in blocks:
                if b.get("status") != "ok":
                    kept = f"; what it captured is kept as {b['file']}" if b.get("file") else ""
                    self.status(f"! segment {b['index']} ended badly (ffmpeg exit {b.get('exit_code')}){kept}")
        catalog.write_sidecar(sess.side, sc)
        self._catalog_session(sess)
        dur = f"{sc['duration_s']:.1f}s" if isinstance(sc["duration_s"], (int, float)) else "?s"
        extra = ""
        if len(blocks) > 1:
            extra = f", {len(blocks)} segments: " + ", ".join(b["file"] for b in blocks if b.get("file"))
        line = events_summary_line(summary)
        if line and not summary.get("whole"):
            extra += f"; {line.replace('  ·  ', ', ')}"
        event(log, logging.INFO, "record.finished", uid=sess.uid, segments=len(blocks), takes=summary["takes"],
              kept_s=summary["kept_s"], total_s=summary["total_s"], events=len(sc["events"]))
        last_rc = (sess.segments[-1][1].get("ffmpeg") or {}).get("exit_code") if sess.segments else None
        return RecordResult(True, f"saved {media} ({dur}){extra}", uid=sess.uid, media_path=media,
                            sidecar_path=sess.side, exit_code=last_rc, duration_s=sc["duration_s"],
                            segments=len(blocks), summary=summary, size_bytes=self._session_size(sess),
                            suffixed_from=sc.get("name_suffixed_from"))

    def _remux(self, ffmpeg: str, capture: Path, final: Path, sc: dict) -> Path:
        """Capture -> MP4 (`-n`: never over an existing file). On failure the Matroska
        is kept (renamed to <stem>.mkv, or <stem>.seg<k>.mkv) and the reason is
        logged, printed and stored in the sidecar (the segment's block)."""
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
        catalog.move_no_clobber(capture, keep)
        sc["remux_error"] = {"exit_code": rc, "stderr_tail": err.strip().splitlines()[-5:]}
        self.status(f"! MP4 remux failed (exit {rc}); kept the Matroska capture: {keep}")
        if final.exists() and final.stat().st_size == 0:
            final.unlink()
        return keep


def fmt_s(seconds: float | None) -> str:
    if not isinstance(seconds, (int, float)):
        return "?"
    s = int(round(seconds))
    return f"{s // 60}:{s % 60:02d}"


def fmt_signed_s(seconds: float | None) -> str:
    """4.2 -> '+4.2 s'; -0.35 -> '-0.3 s' (a moved close, on the status line)."""
    if not isinstance(seconds, (int, float)):
        return "? s"
    return f"{seconds:+.1f} s"


@dataclass
class _Live:
    """The segment being captured right now."""
    index: int
    segsc: dict
    t0: float                 # time.monotonic() at its ffmpeg launch
    started_at: str           # the same instant, ISO (marks' `t` is measured from it)
    proc: FfmpegProcess


@dataclass
class _Session:
    """One recording, across its segments (session C1a)."""
    req: RecordRequest
    sc: dict
    side: Path
    stem: str
    coll_dir: Path
    uid: str
    collection: str
    use_flash: bool
    sources: str
    wanted: tuple
    ffmpeg: str
    final: Path
    model: EventModel
    flasher: object = field(default_factory=NullFlasher)
    segments: list = field(default_factory=list)      # [(block, segsc)] in order
    live: _Live | None = None
    pending: list = field(default_factory=list)       # presses held over a pause / resume
    pause_requested: bool = False
    captured_s: float = 0.0
    reason: str | None = None
    own_side: bool = False         # we created the sidecar (only then may the abort path rewrite it)
    feedback_seq: int = 0          # C1c: one per press published to active.json (the pill shows each once)
    thumb_files: dict = field(default_factory=dict)   # C1d: reference id -> its PNG under the state folder
